import asyncio
import argparse
from abc import ABC, abstractmethod
from typing import (
    List,
    Union,
    Iterator,
    AsyncIterator,
    TYPE_CHECKING,
)

from .helper import AsyncRequestsIterator
from ...logging.logger import JinaLogger
from ...types.message import Message

__all__ = ['BaseStreamer']

if TYPE_CHECKING:
    from ...types.request import Request


class BaseStreamer(ABC):
    """An base async request/response handler"""

    def __init__(
        self,
        args: argparse.Namespace,
    ):
        """
        :param args: args from CLI
        """
        self.args = args
        self.logger = JinaLogger(self.__class__.__name__, **vars(args))

    @property
    @abstractmethod
    def msg_handler(self):
        """
        Property to abstract the entity responsible of handling messages, being an iolet or a connection pool
        """
        ...

    @abstractmethod
    def _convert_to_message(self, request: 'Request') -> Union['Message', 'Request']:
        """Convert request to message

        :param request: current request in the iterator
        """
        ...

    @abstractmethod
    def _handle_request(self, request: 'Request'):
        ...

    def _handle_result(self, result):
        return result

    def _handle_end_of_iter(self) -> None:
        """Send end of iterator signal to Gateway"""
        pass

    @abstractmethod
    async def stream(
        self, request_iterator: Union[Iterator, AsyncIterator]
    ) -> AsyncIterator:
        """iterate through the request iterator and return responses in an async iterator

        :param request_iterator: requests iterator from Client
        """
        ...

    async def _stream_requests(
        self, request_iterator: Union[Iterator, AsyncIterator]
    ) -> AsyncIterator:
        """Implements request and response handling without prefetching

        :param request_iterator: requests iterator from Client
        :yield: responses
        """
        result_queue = asyncio.Queue()
        end_of_iter = asyncio.Event()
        requests_to_handle = set()
        all_requests_handled = asyncio.Event()

        def callback_wrapper(request):
            def callback(future: 'asyncio.Future'):
                """callback to be run after future is completed.
                1. Put the future in the result queue.
                2. Remove the future from futures when future is completed.

                ..note::
                    callback cannot be an awaitable, hence we cannot do `await queue.put(...)` here.
                    We don't add `future.result()` to the queue, as that would consume the exception in the callback,
                    which is difficult to handle.

                :param future: asyncio Future object retured from `handle_response`
                """
                result_queue.put_nowait((request.request_id, future))

            return callback

        async def iterate_requests() -> None:
            """
            1. Traverse through the request iterator.
            2. `add_done_callback` to the future returned by `handle_request`.
                This callback adds the completed future to `result_queue`
            3. Append future to list of futures.
            4. Handle EOI (needed for websocket client)
            5. Set `end_of_iter` event
            """
            async for request in AsyncRequestsIterator(iterator=request_iterator):
                requests_to_handle.add(request.request_id)
                future: 'asyncio.Future' = self._handle_request(request=request)
                future.add_done_callback(callback_wrapper(request))
            self._handle_end_of_iter()
            end_of_iter.set()
            if len(requests_to_handle) == 0:
                all_requests_handled.set()

        asyncio.create_task(iterate_requests())
        get_result_task = None
        if not all_requests_handled.is_set():
            get_result_task = asyncio.create_task(result_queue.get())
        wait_all_requests_handled = asyncio.create_task(all_requests_handled.wait())
        while not all_requests_handled.is_set():
            await asyncio.wait(
                [get_result_task, wait_all_requests_handled],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_result_task.done():
                request_id, future = get_result_task.result()
                yield self._handle_result(future.result())
                requests_to_handle.remove(request_id)
                if len(requests_to_handle) == 0 and end_of_iter.is_set():
                    all_requests_handled.set()
                else:
                    get_result_task = asyncio.create_task(result_queue.get())

    async def _stream_requests_with_prefetch(
        self, request_iterator: Union[Iterator, AsyncIterator], prefetch: int
    ):
        """Implements request and response handling with prefetching

        :param request_iterator: requests iterator from Client
        :param prefetch: number of requests to prefetch
        :yield: response
        """

        async def iterate_requests(
            num_req: int, fetch_to: List[Union['asyncio.Task', 'asyncio.Future']]
        ):
            """
            1. Traverse through the request iterator.
            2. Append the future returned from `handle_request` to `fetch_to` which will later be awaited.

            :param num_req: number of requests
            :param fetch_to: the task list storing requests
            :return: False if append task to `fetch_to` else False
            """
            count = 0
            async for request in AsyncRequestsIterator(iterator=request_iterator):
                fetch_to.append(self._handle_request(request=request))
                count += 1
                if count == num_req:
                    return False
            return True

        prefetch_task = []
        is_req_empty = await iterate_requests(prefetch, prefetch_task)
        if is_req_empty and not prefetch_task:
            self.logger.error(
                'receive an empty stream from the client! '
                'please check your client\'s inputs, '
                'you can use "Client.check_input(inputs)"'
            )
            return

        # the total num requests < prefetch
        if is_req_empty:
            for r in asyncio.as_completed(prefetch_task):
                res = await r
                yield self._handle_result(res)
        else:
            # if there are left over (`else` clause above is unnecessary for code but for better readability)
            onrecv_task = []
            # the following code "interleaves" prefetch_task and onrecv_task, when one dries, it switches to the other
            while prefetch_task:
                if self.logger.debug_enabled:
                    if hasattr(self.msg_handler, 'msg_sent') and hasattr(
                        self.msg_handler, 'msg_recv'
                    ):
                        self.logger.debug(
                            f'send: {self.msg_handler.msg_sent} '
                            f'recv: {self.msg_handler.msg_recv} '
                            f'pending: {self.msg_handler.msg_sent - self.msg_handler.msg_recv}'
                        )
                onrecv_task.clear()
                for r in asyncio.as_completed(prefetch_task):
                    res = await r
                    yield self._handle_result(res)
                    if not is_req_empty:
                        is_req_empty = await iterate_requests(1, onrecv_task)

                # this list dries, clear it and feed it with on_recv_task
                prefetch_task.clear()
                prefetch_task = [j for j in onrecv_task]
