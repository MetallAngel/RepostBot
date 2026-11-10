import io,json,unittest
from email.message import Message
from repostbot.app import Handler
class WebTests(unittest.TestCase):
    def call(self, method, path, data=None, cookie='', raw=False, binary=False, host='localhost:8787', origin='', csrf='1'):
        h = object.__new__(Handler)
        h.server = self.server
        h.path = path
        h.command = method
        h.wfile = io.BytesIO()
        payload = data if raw else json.dumps(data or {}).encode()
        h.rfile = io.BytesIO(payload)
        h.headers = Message()
        h.headers['Content-Length'] = str(len(payload))
        h.headers['X-Repostbot-Request'] = csrf
        h.headers['Host'] = host
        if origin:
            h.headers['Origin'] = origin
        if cookie:
            h.headers['Cookie'] = cookie
        status = []
        response_headers = {}
        h.send_response = lambda code: status.append(code)
        h.send_header = lambda key, value: response_headers.__setitem__(key, value)
        h.end_headers = lambda: None
        if method == 'POST':
            h.do_POST()
        else:
            h.do_GET()
        return (status[0], response_headers, h.wfile.getvalue() if binary else json.loads(h.wfile.getvalue()))
