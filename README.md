# astrolabe

## Python 日志

公共日志封装位于 `common/log.py`，仅依赖 Python 标准库，最低支持 Python 3.8。
当前尚未配置 Python 包安装；以下示例从仓库根目录执行，使用 `common.log` 导入。

```python
import tempfile
from pathlib import Path

from common.log import Log

log_dir = Path(tempfile.gettempdir()) / "astrolabe" / "logs"
with Log("astrolabe", level=Log.info, path=log_dir) as log:
    logger = log.get_logger()
    logger.info("Starting extraction type=%s", "SecurityTick")
    # logger.exception("Extraction failed") inside an exception handler.
```

普通日志格式示例：

```text
I2026-09-19 10:00:00.123+0800 12345:67890 extract.py:main:42] Starting extraction type=SecurityTick
```

- 默认级别为 `INFO`，控制台输出到 `stderr`；`to_console=False` 关闭控制台。
- `path` 是日志目录，默认 `None` 不写文件；显式提供时自动创建目录。
  运行产物应放在源码树之外，长期保留时使用显式配置的产物目录。
- 文件采用 UTF-8，名称为 `name.YYYYMMDD.HHMMSS.ffffff.PID.log`。
  独占创建，名称碰撞或目录不可写时明确报错，不覆盖或混写已有文件。
- 时间使用记录创建时刻和本地时区偏移；业务时间另在消息中显式记录。
- 关闭标准库的进程／线程信息采集时，相应 PID／TID 显示为 `-`，日志仍正常输出。
- `logger.exception()`、`exc_info` 和 `stack_info` 保留完整堆栈。
- 在程序入口配置一次。业务模块使用
  `logging.getLogger("astrolabe.data.hdb.extract")` 等子 logger 继承配置，
  不自行添加 handlers 或设置级别。
- 同名同配置的 `Log` 共享 handlers；配置不同或已有外部 handlers 时拒绝初始化。
  `set_level()` 修改同名 logger 的共享级别。最后一个实例 `close()` 时释放资源，
  恢复该 logger 原有级别和传播设置；重复关闭安全，也可使用 `with`。
- `with` 中已有业务异常时，日志关闭失败不会替换业务异常；关闭错误尝试写入
  `stderr`，失败时回退到原始 `sys.__stderr__`。两者都不可用时优先保留业务异常。
  没有业务异常时，关闭错误照常抛出；直接调用 `close()` 也会抛出关闭错误。
  清理时逐个尝试所有自有 handlers 的 flush 和 close，发生多个错误时报告第一个。
- 不修改 root logger，import 不创建文件、不配置输出。
  关闭前应停止使用该 logger 的任务；关闭后业务代码不再持有或使用返回的 logger。
- 初始化、调整级别和关闭由程序控制线程执行；工作线程使用标准 logger 写日志。
  多进程各自在进程创建后初始化日志，不继承已经打开的文件 handler。
- 当前使用同步 handlers，不做轮转或异步队列；日志用于诊断，不替代审计账本或结果校验。

在仓库根目录验证日志模块：

```bash
python3 -m unittest discover -s tests/common -p 'test_*.py' -v
```

现有 HDB Python 3.8 环境的验证入口（macOS 上通过 OrbStack）：

```bash
orb -m ubuntu2404 bash -lc 'cd /Users/liuxiang/dev/astrolabe && /home/liuxiang/.local/python-3.8.20/bin/python3.8 -B -m unittest discover -s tests/common -p "test_*.py" -v'
```
