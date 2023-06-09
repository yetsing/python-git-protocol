import abc
import argparse
import collections
import gzip
import itertools
import os
import pathlib
import socket
import subprocess
import threading
import typing as t
from wsgiref.simple_server import make_server

import logutil

logger = logutil.get_logger(__name__)


class HTTPRequest:
    def __init__(self):
        self.method = ""
        # 不带 query 的路径
        self.path_info = ""
        self.query_string = ""
        self.query: t.Dict[str, t.List[str]] = {}
        self.headers: t.Dict[str, str] = {}

        self.server_protocol = ""

        self.content_type: t.Optional[str] = None
        self.content_length: t.Optional[int] = None

        self.wsgi_environ = {}

        self.accept: t.Optional[str] = None

        self.stream: t.Optional[t.BinaryIO] = None

    def parse_query_string(self):
        if not self.query_string:
            return
        for part in self.query_string.split("&"):
            k, v = part.split("=", 1)
            self.query.setdefault(k, []).append(v)

    def parse_header(self):
        environ = self.wsgi_environ
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                header_key = key[len("HTTP_"):]
                self.headers[header_key] = value

    def from_query(self, key: str) -> t.Optional[str]:
        if key not in self.query:
            return
        return self.query[key][0]

    def from_querys(self, key: str) -> t.List[str]:
        return self.query.get(key, [])

    def from_header(self, key: str) -> t.Optional[str]:
        key = key.upper().replace("-", "_")
        return self.headers.get(key)

    @classmethod
    def from_wsgi_environ(cls, environ: dict) -> "HTTPRequest":
        req = cls()
        req.method = environ["REQUEST_METHOD"]
        req.path_info = environ["PATH_INFO"]
        req.query_string = environ.get("QUERY_STRING", "")
        req.parse_query_string()
        req.content_type = environ.get("CONTENT_TYPE")
        content_length = environ.get("CONTENT_LENGTH")
        if content_length:
            req.content_length = int(content_length)
        req.server_protocol = environ["SERVER_PROTOCOL"]
        req.wsgi_environ = environ
        req.parse_header()
        req.stream = environ["wsgi.input"]
        req.accept = req.from_header("accept")
        return req


class HTTPResponse:
    code = 200
    reason = "OK"

    def __init__(
            self, body: t.Optional[bytes] = None, status_code: t.Optional[int] = None
    ):
        self.status_code = status_code or self.code
        self.status = f"{self.status_code} {self.reason}"
        self.body = body

        self.data_iter: t.Optional[t.Iterable] = None

        self.headers: t.Dict[str, t.List[str]] = {}

        self._iter: t.Iterator[bytes] = iter((b"",))

        if self.body is not None:
            self.add_header("Content-Length", str(len(self.body)))

    def add_header(self, key: str, value: str):
        self.headers.setdefault(key, []).append(value)

    def set_content_type(self, value: str):
        self.add_header("Content-Type", value)

    def set_data_iter(self, data_iter: t.Iterable[bytes]):
        if self.body is not None:
            raise ValueError("only set one between body and data_iter")
        self.data_iter = data_iter

    def get_headers(self) -> t.Dict[str, str]:
        h = {}
        for k, v in self.headers.items():
            sv = ",".join(v)
            h[k] = sv
        return h

    # 支持迭代器
    def __iter__(self):
        if self.body is not None:
            self._iter = iter((self.body,))
        if self.data_iter is not None:
            self._iter = iter(self.data_iter)
        return self

    def __next__(self):
        return next(self._iter)


class HTTPInternalServerError(HTTPResponse):
    status_code = 500
    reason = "Internal Server Error"


class HTTPForbidden(HTTPResponse):
    status_code = 403
    reason = "Forbidden"


class HTTPMethodNotAllowed(HTTPResponse):
    status_code = 405
    reason = "Method Not Allowed"


# 这个数字是参照的 subprocess.py 里面读取的大小
POPEN_READ_SIZE = 32768


class LimitedFile:
    """读取最多 maxsize 大小的数据"""

    def __init__(self, fd, maxsize):
        self.fd = fd
        self.maxsize = maxsize
        self.remain = maxsize

    def read(self, size: int) -> bytes:
        if size <= self.remain:
            try:
                data = self.fd.read(size)
            except socket.error:
                raise IOError(self)
            self.remain -= size
        elif self.remain:
            data = self.fd.read(self.remain)
            self.remain = 0
        else:
            data = b""
        return data

    def __repr__(self):
        return "<LimitedFile %s len: %s, read: %s>" % (
            self.fd,
            self.maxsize,
            self.maxsize - self.remain,
        )


def try_utf8_decode(b: bytes) -> t.Union[str, bytes]:
    """尝试进行 Utf8 解码，如果成功，返回字符串，否则返回原来的字节串"""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b


def iochunker_copy(src: t.IO, dst: t.IO, chunk_size: int = POPEN_READ_SIZE):
    """
    每次从 src 中读取 chunk_size 大小数据，写入 dst ，直至读取完 src 全部数据。

    Args:
        src: 负责数据读取
        dst: 负责数据写入
        chunk_size: 每次读取和写入数据的大小， 32768 这个值是参照的 subprocess.py 里面的读取大小
    """
    read_size = 0
    while True:
        b = src.read(chunk_size)
        if not b:
            break
        read_size += len(b)
        dst.write(b)


class PopenIterWrapper:
    """通过迭代的方式获取子进程的标准输出，并等待子进程结束"""

    def __init__(self, popen: subprocess.Popen):
        self._popen = popen
        self._stderr_queue = collections.deque()

        td = threading.Thread(target=self._read_stderr, daemon=True)
        td.start()

    def _read_stderr(self):
        while True:
            try:
                b = self._popen.stderr.read(POPEN_READ_SIZE)
            except:
                logger.exception("Error in reading from Popen.stderr")
                break
            self._stderr_queue.append(b)
            if not b:
                break
        self._popen.stderr.close()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        data: bytes = self._popen.stdout.read(POPEN_READ_SIZE)
        if data:
            return data

        # stdout read finish
        returncode = self._popen.wait()
        if returncode:
            cmd = " ".join(self._popen.args)
            error_msg = try_utf8_decode(b"".join(self._stderr_queue))
            logger.error("fail to execute '%s': %s - %s", cmd, returncode, error_msg)
        raise StopIteration("popen exit")


def write_stdin(input_stream: t.BinaryIO, popen: subprocess.Popen):
    """从 input_stream 读取数据，写入子进程 popen 的标准输入中，读完数据后，关闭子进程 popen 的标准输入"""
    try:
        iochunker_copy(input_stream, popen.stdin)
        popen.stdin.close()
    except:
        logger.exception("Error in writing to Popen.stdin")


class GitApplication:
    git_folder_signature = frozenset(["config", "head", "info", "objects", "refs"])
    commands = frozenset(["git-upload-pack", "git-receive-pack"])

    def __init__(self, content_path: pathlib.Path):
        self.content_path = content_path
        self.valid_accepts = ["application/x-%s-result" % c for c in self.commands]

    def inforefs(
            self, request: "HTTPRequest", repo_path: pathlib.Path
    ) -> "HTTPResponse":
        """WSGI Response producer for HTTP GET Git Smart HTTP /info/refs request."""
        git_command = request.from_query("service")
        if git_command not in self.commands:
            return HTTPMethodNotAllowed()

        service = git_command[len("git-"):]
        # 响应的开头部分数据需要我们自己生成写入
        smart_server_advert = "# service=git-%s\n0000" % service
        start_value = f"{len(smart_server_advert):04x}{smart_server_advert}".encode()

        popen = subprocess.Popen(
            [
                "git",
                service,
                "--stateless-rpc",
                "--advertise-refs",
                str(repo_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out = itertools.chain((start_value,), PopenIterWrapper(popen))
        resp = HTTPResponse()
        resp.set_content_type("application/x-%s-advertisement" % str(git_command))
        resp.set_data_iter(out)
        return resp

    def backend(self, request: "HTTPRequest", repo_path: pathlib.Path):
        """
        处理 clone pull push 对应的 Git smart HTTP 请求
        读取请求数据，喂给 git 子进程，返回子进程的标准输出的内容

        clone pull 对应 git-upload-pack
        push 对应 git-receive-pack
        """
        git_command = request.path_info.rsplit("/", 1)[-1]
        print('git_command', git_command)
        if git_command not in self.commands:
            return HTTPMethodNotAllowed()

        service = git_command[len("git-"):]
        if request.content_length:
            print("request.content_length", request.content_length)
            inputstream = LimitedFile(request.stream, request.content_length)
        else:
            inputstream = request.stream

        if request.from_header("CONTENT_ENCODING") == "gzip":
            inputstream = gzip.GzipFile(fileobj=inputstream)

        popen = subprocess.Popen(
            ["git", service, "--stateless-rpc", str(repo_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        td = threading.Thread(
            target=write_stdin, args=(inputstream, popen), daemon=True
        )
        td.start()
        out = PopenIterWrapper(popen)

        # if git_command in ["git-receive-pack"]:
        #     # updating refs manually after each push. Needed for pre-1.7.0.4 git clients using regular HTTP mode.
        #     subprocess.call(
        #         'git --git-dir "%s" update-server-info' % self.content_path, shell=True
        #     )

        resp = HTTPResponse()
        resp.set_content_type("application/x-%s-result" % service)
        resp.set_data_iter(out)
        return resp

    def __call__(self, environ: t.Dict, start_response: t.Callable):
        logger.info("Active thread count: %s", threading.active_count())
        request = HTTPRequest.from_wsgi_environ(environ)
        # 支持请求 path 不带 .git 后缀
        # example: /GitWeb/info/refs
        # example: /GitWeb.git/info/refs
        empty, repo_name, remainder = request.path_info.split("/", 2)
        if repo_name.endswith(".git"):
            repo_name = repo_name[: -len(".git")]
        repo_path = self.content_path / repo_name
        repo_path = repo_path.resolve()
        logger.info(
            "request repo: %s, path_info: %s, accept: %s", str(repo_path), request.path_info, request.accept,
        )
        if remainder == "info/refs":
            resp = self.inforefs(request, repo_path)
        elif request.accept in self.valid_accepts:
            resp = self.backend(request, repo_path)
        else:
            resp = HTTPForbidden()

        # 设置不要缓存
        resp.add_header("Expires", "Fri, 01 Jan 1980 00:00:00 GMT")
        resp.add_header("Pragma", "no-cache")
        resp.add_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        start_response(resp.status, list(resp.get_headers().items()))
        return resp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', '-d', default=os.getcwd(),
                        help='Specify git repo root directory '
                             '[default:current directory]')
    args = parser.parse_args()

    repo_root = pathlib.Path(args.directory).resolve()
    app = GitApplication(repo_root)
    host = "127.0.0.1"
    port = 8002
    server = make_server(host, port, app)
    logger.info("Listen at %s:%s", host, port)
    logger.info("Git serve directory: %s", args.directory)
    server.serve_forever()


if __name__ == "__main__":
    main()
