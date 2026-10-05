# resp

一个只依赖 Python 标准库的 RESP 线格式解析器与命令状态机内核。它不监听端口，
只处理字节流：把客户端送来的字节交给解析器，解析出的命令在内存键值存储上执行，
返回的字节可以直接写回连接。

- 支持的写法：内联命令与数组形式的 bulk string 命令
- 支持的命令：SET（可带 EX 选项）、GET、DEL、EXPIRE、INCR
- 带过期时间的键：惰性删除与时钟注入

## 目录

- resp/core.py：解析器、键值存储、命令执行与会话
- tests/test_core.py：行为测试

## 跑测试

在项目根目录执行：

    python3 -m unittest discover -s tests -v
