import copy
import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from functools import partial
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Type,
    Union,
)

from pydantic import Field, PrivateAttr
from typing_extensions import Self

from prefect.context import ContextModel
from prefect.exceptions import MissingContextError, SerializationError
from prefect.logging.loggers import get_logger, get_run_logger
from prefect.records import RecordStore
from prefect.records.base import TransactionRecord
from prefect.results import (
    BaseResult,
    ResultRecord,
    ResultStore,
    should_persist_result,
)
from prefect.utilities.annotations import NotSet
from prefect.utilities.collections import AutoEnum
from prefect.utilities.engine import _get_hook_name


class IsolationLevel(AutoEnum):
    READ_COMMITTED = AutoEnum.auto()
    SERIALIZABLE = AutoEnum.auto()


class CommitMode(AutoEnum):
    EAGER = AutoEnum.auto()
    LAZY = AutoEnum.auto()
    OFF = AutoEnum.auto()


class TransactionState(AutoEnum):
    PENDING = AutoEnum.auto()
    ACTIVE = AutoEnum.auto()
    STAGED = AutoEnum.auto()
    COMMITTED = AutoEnum.auto()
    ROLLED_BACK = AutoEnum.auto()


class Transaction(ContextModel):
    """
    A base model for transaction state.
    """

    store: Union[RecordStore, ResultStore, None] = None
    key: Optional[str] = None
    children: List["Transaction"] = Field(default_factory=list)
    commit_mode: Optional[CommitMode] = None
    isolation_level: Optional[IsolationLevel] = IsolationLevel.READ_COMMITTED
    state: TransactionState = TransactionState.PENDING
    on_commit_hooks: List[Callable[["Transaction"], None]] = Field(default_factory=list)
    on_rollback_hooks: List[Callable[["Transaction"], None]] = Field(
        default_factory=list
    )
    overwrite: bool = False
    logger: Union[logging.Logger, logging.LoggerAdapter] = Field(
        default_factory=partial(get_logger, "transactions")
    )
    write_on_commit: bool = True
    _stored_values: Dict[str, Any] = PrivateAttr(default_factory=dict)
    _staged_value: Any = None
    __var__: ContextVar = ContextVar("transaction")

    def set(self, name: str, value: Any) -> None:
        self._stored_values[name] = value

    def get(self, name: str, default: Any = NotSet) -> Any:
        if name not in self._stored_values:
            if default is not NotSet:
                return default
            raise ValueError(f"Could not retrieve value for unknown key: {name}")
        return self._stored_values.get(name)

    def is_committed(self) -> bool:
        return self.state == TransactionState.COMMITTED

    def is_rolled_back(self) -> bool:
        return self.state == TransactionState.ROLLED_BACK

    def is_staged(self) -> bool:
        return self.state == TransactionState.STAGED

    def is_pending(self) -> bool:
        return self.state == TransactionState.PENDING

    def is_active(self) -> bool:
        return self.state == TransactionState.ACTIVE

    def __enter__(self):
        if self._token is not None:
            raise RuntimeError(
                "Context already entered. Context enter calls cannot be nested."
            )
        parent = get_transaction()
        if parent:
            self._stored_values = copy.deepcopy(parent._stored_values)
        # set default commit behavior; either inherit from parent or set a default of eager
        if self.commit_mode is None:
            self.commit_mode = parent.commit_mode if parent else CommitMode.LAZY
        # set default isolation level; either inherit from parent or set a default of read committed
        if self.isolation_level is None:
            self.isolation_level = (
                parent.isolation_level if parent else IsolationLevel.READ_COMMITTED
            )

        assert self.isolation_level is not None, "Isolation level was not set correctly"
        if (
            self.store
            and self.key
            and not self.store.supports_isolation_level(self.isolation_level)
        ):
            raise ValueError(
                f"Isolation level {self.isolation_level.name} is not supported by record store type {self.store.__class__.__name__}"
            )

        # this needs to go before begin, which could set the state to committed
        self.state = TransactionState.ACTIVE
        self.begin()
        self._token = self.__var__.set(self)
        return self

    def __exit__(self, *exc_info):
        exc_type, exc_val, _ = exc_info
        if not self._token:
            raise RuntimeError(
                "Asymmetric use of context. Context exit called without an enter."
            )
        if exc_type:
            self.rollback()
            self.reset()
            raise exc_val

        if self.commit_mode == CommitMode.EAGER:
            self.commit()

        # if parent, let them take responsibility
        if self.get_parent():
            self.reset()
            return

        if self.commit_mode == CommitMode.OFF:
            # if no one took responsibility to commit, rolling back
            # note that rollback returns if already committed
            self.rollback()
        elif self.commit_mode == CommitMode.LAZY:
            # no one left to take responsibility for committing
            self.commit()

        self.reset()

    def begin(self):
        if (
            self.store
            and self.key
            and self.isolation_level == IsolationLevel.SERIALIZABLE
        ):
            self.logger.debug(f"Acquiring lock for transaction {self.key!r}")
            self.store.acquire_lock(self.key)
        if (
            not self.overwrite
            and self.store
            and self.key
            and self.store.exists(key=self.key)
        ):
            self.state = TransactionState.COMMITTED

    def read(self) -> Union["BaseResult", ResultRecord, None]:
        if self.store and self.key:
            record = self.store.read(key=self.key)
            if isinstance(record, ResultRecord):
                return record
            # for backwards compatibility, if we encounter a transaction record, return the result
            # This happens when the transaction is using a `ResultStore`
            if isinstance(record, TransactionRecord):
                return record.result
        return None

    def reset(self) -> None:
        parent = self.get_parent()

        if parent:
            # parent takes responsibility
            parent.add_child(self)

        if self._token:
            self.__var__.reset(self._token)
            self._token = None

        # do this below reset so that get_transaction() returns the relevant txn
        if parent and self.state == TransactionState.ROLLED_BACK:
            parent.rollback()

    def add_child(self, transaction: "Transaction") -> None:
        self.children.append(transaction)

    def get_parent(self) -> Optional["Transaction"]:
        prev_var = getattr(self._token, "old_value")
        if prev_var != Token.MISSING:
            parent = prev_var
        else:
            parent = None
        return parent

    def commit(self) -> bool:
        if self.state in [TransactionState.ROLLED_BACK, TransactionState.COMMITTED]:
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key)

            return False

        try:
            for child in self.children:
                child.commit()

            for hook in self.on_commit_hooks:
                self.run_hook(hook, "commit")

            if self.store and self.key and self.write_on_commit:
                if isinstance(self.store, ResultStore):
                    if isinstance(self._staged_value, BaseResult):
                        self.store.write(
                            key=self.key, obj=self._staged_value.get(_sync=True)
                        )
                    elif isinstance(self._staged_value, ResultRecord):
                        self.store.persist_result_record(
                            result_record=self._staged_value
                        )
                    else:
                        self.store.write(key=self.key, obj=self._staged_value)
                else:
                    self.store.write(key=self.key, result=self._staged_value)

            self.state = TransactionState.COMMITTED
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key)
            return True
        except SerializationError as exc:
            if self.logger:
                self.logger.warning(
                    f"Encountered an error while serializing result for transaction {self.key!r}: {exc}"
                    " Code execution will continue, but the transaction will not be committed.",
                )
            self.rollback()
            return False
        except Exception:
            if self.logger:
                self.logger.exception(
                    f"An error was encountered while committing transaction {self.key!r}",
                    exc_info=True,
                )
            self.rollback()
            return False

    def run_hook(self, hook, hook_type: str) -> None:
        hook_name = _get_hook_name(hook)
        # Undocumented way to disable logging for a hook. Subject to change.
        should_log = getattr(hook, "log_on_run", True)

        if should_log:
            self.logger.info(f"Running {hook_type} hook {hook_name!r}")

        try:
            hook(self)
        except Exception as exc:
            if should_log:
                self.logger.error(
                    f"An error was encountered while running {hook_type} hook {hook_name!r}",
                )
            raise exc
        else:
            if should_log:
                self.logger.info(
                    f"{hook_type.capitalize()} hook {hook_name!r} finished running successfully"
                )

    def stage(
        self,
        value: Any,
        on_rollback_hooks: Optional[List] = None,
        on_commit_hooks: Optional[List] = None,
    ) -> None:
        """
        Stage a value to be committed later.
        """
        on_commit_hooks = on_commit_hooks or []
        on_rollback_hooks = on_rollback_hooks or []

        if self.state != TransactionState.COMMITTED:
            self._staged_value = value
            self.on_rollback_hooks += on_rollback_hooks
            self.on_commit_hooks += on_commit_hooks
            self.state = TransactionState.STAGED

    def rollback(self) -> bool:
        if self.state in [TransactionState.ROLLED_BACK, TransactionState.COMMITTED]:
            return False

        try:
            for hook in reversed(self.on_rollback_hooks):
                self.run_hook(hook, "rollback")

            self.state = TransactionState.ROLLED_BACK

            for child in reversed(self.children):
                child.rollback()

            return True
        except Exception:
            if self.logger:
                self.logger.exception(
                    f"An error was encountered while rolling back transaction {self.key!r}",
                    exc_info=True,
                )
            return False
        finally:
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key)

    @classmethod
    def get_active(cls: Type[Self]) -> Optional[Self]:
        return cls.__var__.get(None)


def get_transaction() -> Optional[Transaction]:
    return Transaction.get_active()


@contextmanager
def transaction(
    key: Optional[str] = None,
    store: Union[RecordStore, ResultStore, None] = None,
    commit_mode: Optional[CommitMode] = None,
    isolation_level: Optional[IsolationLevel] = None,
    overwrite: bool = False,
    write_on_commit: Optional[bool] = None,
    logger: Union[logging.Logger, logging.LoggerAdapter, None] = None,
) -> Generator[Transaction, None, None]:
    """
    A context manager for opening and managing a transaction.

    Args:
        - key: An identifier to use for the transaction
        - store: The store to use for persisting the transaction result. If not provided,
            a default store will be used based on the current run context.
        - commit_mode: The commit mode controlling when the transaction and
            child transactions are committed
        - overwrite: Whether to overwrite an existing transaction record in the store
        - write_on_commit: Whether to write the result to the store on commit. If not provided,
            will default will be determined by the current run context. If no run context is
            available, the value of `PREFECT_RESULTS_PERSIST_BY_DEFAULT` will be used.

    Yields:
        - Transaction: An object representing the transaction state
    """
    # if there is no key, we won't persist a record
    if key and not store:
        from prefect.context import FlowRunContext, TaskRunContext
        from prefect.results import ResultStore, get_default_result_storage

        flow_run_context = FlowRunContext.get()
        task_run_context = TaskRunContext.get()
        existing_store = getattr(task_run_context, "result_store", None) or getattr(
            flow_run_context, "result_store", None
        )

        new_store: ResultStore
        if existing_store and existing_store.result_storage_block_id:
            new_store = existing_store.model_copy(
                update={
                    "persist_result": True,
                }
            )
        else:
            default_storage = get_default_result_storage(_sync=True)
            if existing_store:
                new_store = existing_store.model_copy(
                    update={
                        "persist_result": True,
                        "storage_block": default_storage,
                        "storage_block_id": default_storage._block_document_id,
                    }
                )
            else:
                new_store = ResultStore(
                    result_storage=default_storage,
                )
        from prefect.records.result_store import ResultRecordStore

        store = ResultRecordStore(
            result_store=new_store,
        )

    try:
        logger = logger or get_run_logger()
    except MissingContextError:
        logger = get_logger("transactions")

    with Transaction(
        key=key,
        store=store,
        commit_mode=commit_mode,
        isolation_level=isolation_level,
        overwrite=overwrite,
        write_on_commit=write_on_commit
        if write_on_commit is not None
        else should_persist_result(),
        logger=logger,
    ) as txn:
        yield txn
