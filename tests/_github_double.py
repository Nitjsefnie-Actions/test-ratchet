"""A local GitHub REST API for tests: it answers the routes a test gives it, records every
request, and fails the test on any request it does not model."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

# A route takes the request's JSON body and answers (status, JSON body, extra headers).
Route = Callable[[Any], Tuple[int, Any, Dict[str, str]]]


class Request(NamedTuple):
    method: str
    path: str
    body: Any
    headers: Dict[str, str]


def answer(value: Any, status: int = 200, link: Optional[str] = None) -> Route:
    return lambda _body: (status, value, {'Link': f'<{link}>; rel="next"'} if link else {})


class GitHub:
    def __init__(self, test: unittest.TestCase, routes: Dict[Tuple[str, str], Route]) -> None:
        self.routes = routes
        self.requests: List[Request] = []
        self.unmodelled: List[str] = []
        double = self

        class Handler(BaseHTTPRequestHandler):
            def serve(self) -> None:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                double.requests.append(Request(self.command, self.path, body,
                                               {name.lower(): text for name, text
                                                in self.headers.items()}))
                route = double.routes.get((self.command, self.path))
                if route is None:
                    double.unmodelled.append(f'{self.command} {self.path}')
                    status, value, headers = 501, {'message': 'not modelled'}, {}
                else:
                    status, value, headers = route(body)
                data = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                for name, text in {**headers, 'Content-Type': 'application/json',
                                   'Content-Length': str(len(data))}.items():
                    self.send_header(name, text)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = serve

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        test.addCleanup(lambda: test.assertEqual(self.unmodelled, [], 'unmodelled requests'))
        test.addCleanup(thread.join)
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    def calls(self) -> List[str]:
        return [f'{request.method} {request.path}' for request in self.requests]
