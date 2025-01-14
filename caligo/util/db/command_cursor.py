import asyncio
from collections import deque
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Coroutine,
    Deque,
    MutableMapping,
    Optional,
    Tuple,
    Union
)

from pymongo.command_cursor import CommandCursor as _CommandCursor

from .client_session import AsyncClientSession
from .cursor_base import AsyncCursorBase

from caligo import util

if TYPE_CHECKING:
    from .collection import AsyncCollection


class CommandCursor(_CommandCursor):

    _CommandCursor__data: Deque[Any]
    _CommandCursor__killed: bool

    delegate: "AsyncCollection"

    def __init__(
        self,
        collection: "AsyncCollection",
        cursor_info: MutableMapping[str, Any],
        address: Optional[Tuple[str, int]] = None,
        *,
        batch_size: int = 0,
        max_await_time_ms: Optional[int] = None,
        session: Optional[AsyncClientSession] = None,
        explicit_session: bool = False,
    ) -> None:
        self.delegate = collection

        super().__init__(
            collection.dispatch,
            cursor_info,
            address,
            batch_size=batch_size,
            max_await_time_ms=max_await_time_ms,
            session=session.dispatch if session else session,
            explicit_session=explicit_session,
        )


    async def _AsyncCommandCursor__die(self, synchronous: bool = False) -> None:
        await util.run_sync(self.__die, synchronous=synchronous)

    @property
    def _AsyncCommandCursor__data(self) -> Deque[Any]:
        return self.__data

    @property
    def _AsyncCommandCursor__killed(self) -> bool:
        return self.__killed

    @property
    def collection(self) -> "AsyncCollection":
        return self.__collection


class RawBatchCommandCursor(CommandCursor):
    pass


class AsyncCommandCursor(AsyncCursorBase):
    """AsyncIO :obj:`~CommandCursor`

       *DEPRECATED* methods are removed in this class.
    """

    dispatch: CommandCursor

    def _query_flags(self) -> int:
        return 0

    def _data(self) -> Deque[Any]:
        return self.dispatch._CommandCursor__data  # skipcq: PYL-W0212

    def _killed(self) -> bool:
        return self.dispatch._CommandCursor__killed  # skipcq: PYL-W0212


class _LatentCursor:
    """Base class for LatentCursor AsyncIOMongoDB instance"""
    # ClassVar
    alive: ClassVar[bool] = True
    _CommandCursor__data: ClassVar[Deque[Any]] = deque()
    _CommandCursor__id: ClassVar[Optional[Any]] = None
    _CommandCursor__killed: ClassVar[bool] = False
    _CommandCursor__sock_mgr: ClassVar[Optional[Any]] = None
    _CommandCursor__session: ClassVar[Optional[AsyncClientSession]] = None
    _CommandCursor__explicit_session: ClassVar[Optional[bool]] = None
    address: ClassVar[Optional[Tuple[str, int]]] = None
    cursor_id: ClassVar[Optional[Any]] = None
    session: ClassVar[Optional[AsyncClientSession]] = None

    _CommandCursor__collection: "AsyncCollection"

    def __init__(self, collection: "AsyncCollection") -> None:
        self._CommandCursor__collection = collection

    def _CommandCursor__end_session(self, *args: Any, **kwargs: Any) -> None:
        pass  # Only for initialization

    def _CommandCursor__die(self, *args: Any, **kwargs: Any) -> None:
        pass  # Only for initialization

    def _refresh(self) -> int:  # skipcq: PYL-R0201
        """Only for initialization"""
        return 0

    def batch_size(self, batch_size: int) -> None:
        pass  # Only for initialization

    def close(self) -> None:
        pass  # Only for initialization

    def clone(self) -> "_LatentCursor":
        return _LatentCursor(self._CommandCursor__collection)

    def rewind(self):
        pass  # Only for initialization

    @property
    def collection(self):
        return self._CommandCursor__collection


class AsyncLatentCommandCursor(AsyncCommandCursor):
    """Temporary Cursor for initializing in aggregate,
       and will be overwrite by :obj:`~asyncio.Future`"""

    dispatch: Union[CommandCursor, RawBatchCommandCursor]

    def __init__(
        self,
        collection: "AsyncCollection",
        start: Callable[..., Union[CommandCursor, RawBatchCommandCursor]],
        *args: Any,
        **kwargs: Any
    ) -> None:
        self.start = start
        self.args = args
        self.kwargs = kwargs

        super().__init__(_LatentCursor(collection), collection)

    def batch_size(self, batch_size: int) -> "AsyncLatentCommandCursor":
        self.kwargs["batchSize"] = batch_size
        return self

    def _get_more(self) -> Union[asyncio.Future[int], Coroutine[Any, Any, int]]:
        if not self.started:
            self.started = True
            original_future = self.loop.create_future()
            future = self.loop.create_task(
                util.run_sync(self.start, *self.args, **self.kwargs))
            future.add_done_callback(
                partial(self.loop.call_soon_threadsafe,
                        self._on_started,
                        original_future)
            )

            return original_future

        return super()._get_more()

    def _on_started(
        self,
        original_future: asyncio.Future[int],
        future: asyncio.Future[Union[CommandCursor, RawBatchCommandCursor]]
    ) -> None:
        try:
            self.dispatch = future.result()
        except Exception as exc:  # skipcq: PYL-W0703
            if not original_future.done():
                original_future.set_exception(exc)
        else:
            # Return early if the task was cancelled.
            if original_future.done():
                return

            if self.dispatch._CommandCursor__data or not self.dispatch.alive:  # skipcq: PYL-W0212
                # _get_more is complete.
                original_future.set_result(len(self.dispatch._CommandCursor__data))  # skipcq: PYL-W0212
            else:
                # Send a getMore.
                fut = super()._get_more()
                if isinstance(fut, asyncio.Future):

                    def copy(f: asyncio.Future) -> None:
                        if original_future.done():
                            return

                        exc = f.exception()
                        if exc is not None:
                            original_future.set_exception(exc)
                        else:
                            original_future.set_result(f.result())

                    fut.add_done_callback(copy)
