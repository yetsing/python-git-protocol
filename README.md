# python-git-protocol

python git protocol support

# 运行

- 环境配置

clone 代码到本地。创建 Python 虚拟环境 (>=3.7)

安装依赖

```shell
pip install -r requirements.txt
```

新建仓库用于测试

```shell
mkdir -p /tmp/gitrepo
cd /tmp/gitrepo
git init --bare example
```

> bare 仓库是一种特殊的仓库，主要用于接受 pull 和 push 操作。

- http dumb

启动服务

```shell
wsgidav --host=0.0.0.0 --port=8000 --root=/tmp/gitrepo --auth=anonymous
```

需要先在我们之前创建的仓库上执行下列命令

```shell
git --git-dir "/tmp/gitrepo/example/" update-server-info
```

clone 仓库

```shell
rm -rf /tmp/git_playground/example/
mkdir -p /tmp/git_playground
cd /tmp/git_playground
git clone http://127.0.0.1:8000/example
```

验证 pull 和 push

```shell
cd /tmp/git_playground/example
touch a.txt
git add a.txt
git commit -m "init"
git push
git pull
```

- http smart

启动服务

```shell
python http_smart_server.py -d /tmp/gitrepo
```

clone 仓库

```shell
rm -rf /tmp/git_playground/example/
mkdir -p /tmp/git_playground
cd /tmp/git_playground
git clone http://127.0.0.1:8002/example
```

验证 pull 和 push

```shell
cd /tmp/git_playground/example
echo "http smart" > a.txt
git add a.txt
git commit -m "smart"
git push
git pull
```

- git protocol

启动服务

```shell
python git_server.py -d /tmp/gitrepo
```

clone 仓库

```shell
rm -rf /tmp/git_playground/example/
mkdir -p /tmp/git_playground
cd /tmp/git_playground
git clone git://127.0.0.1:8004/example
```

验证 pull 和 push

```shell
cd /tmp/git_playground/example
echo "git protocol" > a.txt
git add a.txt
git commit -m "git protocol"
git push
git pull
```

- ssh protocol

TODO

# 参考

[聊聊 Git 的三种传输协议及实现](https://zhuanlan.zhihu.com/p/354043577)

[go-git-protocols](https://gitee.com/kesin/go-git-protocols/tree/master)

[GitWeb](https://github.com/gawel/GitWeb)
