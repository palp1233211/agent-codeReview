# Full application runtime using OpenAI: Yunxiao MCP review, Feishu bot, Dify,
# and MySQL are included. It does not install Node.js, Claude Code CLI, or Claude SDK.
FROM python:3.11-slim-bullseye AS openai-runtime

ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

COPY requirements-openai.txt .
RUN pip install --no-cache-dir -r requirements-openai.txt

COPY . .

RUN useradd -m -u 1000 pn \
 && chown -R pn:pn /app
USER pn
ENV HOME=/home/pn

# Default long-running container; invoke concrete jobs with docker exec, for example:
#   docker exec <container> python cli.py yunxiao-mr -r <repo> -m <mr>
CMD ["sleep", "infinity"]


# Full runtime: Python 3.11 + Node 22 for Claude provider and stdio Yunxiao MCP.
# Keep bullseye because older Docker/libseccomp versions can block Node's clone3
# syscall on newer glibc images, causing Node to abort during thread creation.
FROM nikolaik/python-nodejs:python3.11-nodejs22-bullseye AS full-runtime

ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# Claude provider needs the claude CLI. The npm Yunxiao MCP server is installed
# ahead of time so Claude stdio MCP does not depend on runtime npx downloads.
RUN npm config set registry https://registry.npmmirror.com \
 && npm install -g @anthropic-ai/claude-code alibabacloud-devops-mcp-server \
 && npm cache clean --force

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# pn is provided by the base image as uid 1000.
RUN chown -R pn:pn /app
USER pn
ENV HOME=/home/pn

# Default long-running container; invoke concrete jobs with docker exec, for example:
#   docker exec <container> python cli.py yunxiao-mr -r <repo> -m <mr>
CMD ["sleep", "infinity"]
