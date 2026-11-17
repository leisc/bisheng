"""导出演示数据库快照（全库，走 Docker 内的 mysqldump）。

用法:
    .venv/bin/python dump-demo-snapshot.py [输出路径] [--no-data]
    # 或不带 .venv，直接:
    python3 dump-demo-snapshot.py

默认输出 /tmp/bisheng-demo-snapshot.sql。

为什么用脚本而不是直接敲 mysqldump：
- 本机没装 mysql-client，只能 docker exec bisheng-mysql mysqldump；
- 固定 --single-transaction --no-tablespaces --set-gtid-purged=OFF，
  避免 GTID / 表空间在目标库导入时报错；
- 固定 --default-character-set=utf8mb4，避免演示数据里的中文变问号；
- 写入统一的快照头注释，说明导出时间、内容范围与导入命令。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

CONTAINER = 'bisheng-mysql'
DB = 'bisheng'
OUT = '/tmp/bisheng-demo-snapshot.sql'

FLAGS = [
    '--single-transaction', '--quick', '--no-tablespaces',
    '--set-gtid-purged=OFF', '--default-character-set=utf8mb4',
    '--hex-blob', '--skip-add-locks', '--skip-comments',
    '--triggers', '--routines',
]

HEADER = ('-- BiSheng / INSOFT 演示数据快照\n'
          '-- 导出时间: {ts}\n'
          '-- 内容: 账号与组织、模型、知识库与 ES 分块、4 个演示工作流、3 个助手、\n'
          '--       会话记录、品牌配置、工作台配置（含日常对话系统提示词）\n'
          '-- 导入: mysql -uroot -p bisheng < {name}\n'
          '-- 注意: 导入前建议先建空库；本快照含 CREATE TABLE IF NOT EXISTS 之外的\n'
          '--       建表语句，重复导入需先 DROP DATABASE。\n\n')


def main():
    out = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else OUT
    no_data = '--no-data' in sys.argv

    cmd = ['docker', 'exec', CONTAINER, 'mysqldump',
           '-uroot', '-p1234'] + FLAGS + [DB]
    if no_data:
        cmd.append('--no-data')

    print('导出 %s -> %s%s' % (DB, out, '（仅结构）' if no_data else ''))
    # 先把 mysqldump 落到临时文件，再「头注释 + 转储」拼起来写目标文件。
    # 不能像原来那样 f.write(HEADER) 之后直接把 f 交给 subprocess 当 stdout：
    # Python 的写是带缓冲的，头注释还压在缓冲区里没落盘，子进程已经往 fd 的
    # 偏移 0 处写了一整份转储，最后 flush 时头注释被追加到了文件**末尾**，
    # 导入说明就看不到了。
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.sql', prefix='bisheng-dump-')
    try:
        with os.fdopen(tmp_fd, 'wb') as tmp:
            r = subprocess.run(cmd, stdout=tmp, stderr=subprocess.PIPE)
        if r.returncode != 0:
            print('mysqldump 失败:', r.stderr.decode()[:2000])
            return
        with open(out, 'wb') as f:
            f.write(HEADER.format(
                ts=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                name=os.path.basename(out)).encode('utf-8'))
            with open(tmp_path, 'rb') as tmp:
                shutil.copyfileobj(tmp, f)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    size = os.path.getsize(out)
    with open(out, encoding='utf-8') as f:
        n_tables = sum(1 for line in f if line.startswith('CREATE TABLE'))
    print('完成：%d 张表，%.1f KB -> %s' % (n_tables, size / 1024, out))


if __name__ == '__main__':
    main()
