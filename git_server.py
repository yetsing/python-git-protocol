"""
实现 git 传输协议的 server

文档：
https://git-scm.com/docs/pack-protocol#_git_transport

https://zhuanlan.zhihu.com/p/354043577 Git 协议部分

https://git-scm.com/book/en/v2/Git-on-the-Server-The-Protocols The Git Protocol 部分

git clone git://example.com/project.git
"""
import argparse
import dataclasses
import os
import pathlib
import socketserver
import subprocess
import typing as t

import logutil

logger = logutil.get_logger(__name__)

repo_root: t.Optional[pathlib.Path] = None


@dataclasses.dataclass
class GitProtoRequest:
    command: str
    pathname: str
    hostname: str
    port: int
    extra_parameters: t.List[bytes]


class GitTransportHandler(socketserver.StreamRequestHandler):
    timeout: int = 600

    pkt_line_minlength: int = 5
    pkt_line_maxlength: int = 65524

    def pkt_line(self, msg: bytes) -> bytes:
        """
        https://gitlab.obspm.fr/jas/git/blob/b64e1f58158d1d1a8eafabbbf002a1a3c1d72929/Documentation/technical/protocol-common.txt

        A pkt-line with a length field of 0 ("0000"), called a flush-pkt,
        is a special case and MUST be handled differently than an empty
        pkt-line ("0004").

        ----
          pkt-line     =  data-pkt / flush-pkt

          data-pkt     =  pkt-len pkt-payload
          pkt-len      =  4*(HEXDIG)
          pkt-payload  =  (pkt-len - 4)*(OCTET)

          flush-pkt    = "0000"
        ----

        前面四个字节是数据长度加 4 的十六进制表示，后面跟指定长度的数据
        """
        length = len(msg) + 4
        return f"{length:04x}".encode() + msg + b"0000"

    def exit_session(self, error_msg: bytes):
        data = self.pkt_line(b"ERR " + error_msg)
        self.wfile.write(data)

    def handle(self) -> None:
        try:
            self._handle()
        except:
            logger.exception("fail to handle")

    def _handle(self) -> None:
        # 解析初始的数据
        git_request = self.parse_init_data()
        if git_request is None:
            return
        if git_request.command not in ("git-upload-pack", "git-receive-pack"):
            self.exit_session(b"Not allowed command. \n")
            return
        repo_name = git_request.pathname[1:]
        if repo_name.endswith(".git"):
            repo_name = repo_name[: -len(".git")]
        repo_path = repo_root / repo_name
        if not repo_path.exists():
            logger.error("Not found repo_path: '%s'", repo_path)
            self.exit_session(b"Not found repo " + repo_name.encode() + b" \n")
            return

        # 确定客户端使用的协议版本
        version = None
        for param in git_request.extra_parameters:
            if param.startswith(b"version="):
                version = int(param[len(b"version="):].decode())

        env = None
        if version is not None:
            env = {"GIT_PROTOCOL": f"version={version}"}
            # 继承现有进程的环境变量
            env.update(os.environ)
        popen_args = ["git", git_request.command[len("git-"):], str(repo_path)]
        popen = subprocess.Popen(
            popen_args,
            stdin=self.rfile,
            stdout=self.wfile,
            env=env,
        )
        cmd = " ".join(popen_args)
        logger.info("Execute command '%s'", cmd)
        returncode = popen.wait()
        if returncode:
            logger.error("Fail to execute '%s': %s - %s", cmd, returncode)
            self.exit_session(b"Execute error. \n")

    def parse_init_data(self) -> t.Optional[GitProtoRequest]:
        """

        https://git-scm.com/docs/pack-protocol#_git_transport

        request = PKT-LINE(git-proto-request)
        git-proto-request = request-command SP pathname NUL
              [ host-parameter NUL ] [ NUL extra-parameters ]
        request-command   = "git-upload-pack" / "git-receive-pack" /
              "git-upload-archive"   ; case sensitive
        pathname          = *( %x01-ff ) ; exclude NUL
        host-parameter    = "host=" hostname [ ":" port ]
        extra-parameters  = 1*extra-parameter
        extra-parameter   = 1*( %x01-ff ) NUL

        上面是初始数据格式，下面是一些数据的例子
        0033git-upload-pack /project.git\0host=myserver.com\0
        003egit-upload-pack /project.git\0host=myserver.com\0\0version=1\0
        """
        rfile = self.rfile
        # 读取前四个字节的长度
        b = rfile.read(4)
        length = int(b.decode(), 16)
        if length < self.pkt_line_minlength or length > self.pkt_line_maxlength:
            self.exit_session(b"Invalid request data\n")
            return

        data = rfile.read(length - 4)
        params = data.split(b"\0")
        if len(params) < 2:
            self.exit_session(b"Invalid reqeust data\n")
            return

        line = params[0]
        parts = line.split(b" ")
        if len(parts) != 2:
            self.exit_session(b"Invalid request data\n")
            return

        request_command = parts[0].decode()
        pathname = parts[1].decode()
        extra_params = []
        hostname = "127.0.0.1"
        port = 8194
        if len(params) == 2:
            # 例子 b'git-upload-pack /project.git\0'
            if params[1] != b"":
                self.exit_session(b"Invalid request data\n")
                return
        elif len(params) == 3:
            # 例子 b'git-upload-pack /project.git\0host=myserver.com\0'
            if params[2] != b"":
                self.exit_session(b"Invalid request data\n")
                return
            # "host=myserver.com" 或者 "host=myserver.com:8194"
            prefix = "host="
            host_param = params[1].decode()
            if not host_param.startswith(prefix):
                self.exit_session(b"Invalid request host param\n")
                return
            host_parts = host_param.split(":", 1)
            hostname = host_parts[0][len(prefix):]
            if len(host_parts) > 1:
                port = int(host_parts[1])
        else:
            # 存在 extra-params
            # 例子 b"git-upload-pack /project.git\0host=myserver.com\0\0version=1\0"
            if not (params[2] == b"" and params[-1] == b""):
                self.exit_session(b"Invalid request extra params\n")
                return
            extra_params = params[3:-1]
        return GitProtoRequest(
            request_command,
            pathname,
            hostname,
            port,
            extra_params,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', '-d', default=os.getcwd(),
                        help='Specify git repo root directory '
                             '[default:current directory]')
    args = parser.parse_args()

    global repo_root
    repo_root = pathlib.Path(args.directory).resolve()

    host = "127.0.0.1"
    port = 8004
    server_address = (host, port)
    server = socketserver.TCPServer(server_address, GitTransportHandler)
    logger.info("Listen at %s:%s", host, port)
    logger.info("Git serve directory: %s", str(repo_root))
    server.serve_forever()


if __name__ == "__main__":
    main()
