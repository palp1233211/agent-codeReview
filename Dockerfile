# 官方预构建镜像：Python 3.11 + Node 22
# Node 必须 >= 22，@anthropic-ai/claude-code 的 engines 要求
# 必须用 bullseye(Debian 11/glibc 2.31)：bookworm 的 glibc 2.36 走 clone3 系统调用，
# 会被 Docker < 20.10.10 的旧 libseccomp 拦成 EPERM，导致 Node 无法创建线程直接 abort
FROM nikolaik/python-nodejs:python3.11-nodejs22-bullseye

ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# claude CLI 是 claude_agent_sdk 的运行时依赖（SDK 通过子进程调用它）
# 云效 MCP server 提前装好，避免每次运行时 npx 联网下载
RUN npm config set registry https://registry.npmmirror.com \
 && npm install -g @anthropic-ai/claude-code alibabacloud-devops-mcp-server \
 && npm cache clean --force

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# permission_mode="bypassPermissions" 不允许以 root 运行
# pn 是基础镜像自带的 uid 1000 用户
RUN chown -R pn:pn /app
USER pn
ENV HOME=/home/pn

# 默认常驻，不退出；具体任务由外部通过 docker exec 触发：
#   docker exec <container> python cli.py yunxiao-mr -r <repo> -m <mr>
# 也可一次性执行：docker run --rm <image> python cli.py --help
CMD ["sleep", "infinity"]
