FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps . && useradd --uid 10001 --create-home ctwatch && chmod 1777 /tmp
COPY data ./data
COPY config ./config
# ECS copies the image directory permissions into the empty writable volume.
VOLUME ["/tmp"]
USER 10001
CMD ["python", "-m", "ctwatch.main"]
