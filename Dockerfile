# syntax=docker/dockerfile:1
# 前端为零构建静态页（frontend/ 直接随镜像分发），仅需 Python 运行时
FROM python:3.12-slim
ARG UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_INDEX_URL=$UV_INDEX_URL \
    UV_NO_COMPILE_BYTECODE=1

RUN pip install --no-cache-dir -i "$UV_INDEX_URL" uv

WORKDIR /app

# 依赖单独一层：pyproject 不变时可复用构建缓存
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
 && uv pip install --system --no-cache -r /tmp/req.txt \
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
