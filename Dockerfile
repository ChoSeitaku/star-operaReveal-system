# syntax=docker/dockerfile:1
# 前端为零构建静态页（frontend/ 直接随镜像分发），仅需 Python 运行时
FROM python:3.12-slim
ARG UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=$UV_INDEX_URL \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖单独一层：pyproject 不变时可复用构建缓存
# 注：直接用 pip 安装（不再依赖 uv，部分镜像源会拒发 uv 的 wheel），并剔掉 requirements 里的注释与空行
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(x for x in tomllib.load(open('pyproject.toml','rb'))['project']['dependencies'] if x.strip() and not x.strip().startswith('#')))" > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt \
 && rm /tmp/req.txt

COPY . .

# data/（system.db、secret.key）走 named volume；db/demo 为情景库，缺失时启动自动重建
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir -p /app/data /app/db \
 && chown -R app:app /app

USER app

EXPOSE 8000 8001
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"

# run.py 同容器拉起后端(8000)与认证中心(8001)两个进程
CMD ["python", "run.py", "--host", "0.0.0.0"]
