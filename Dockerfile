FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY app/ ./app/

ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/4EVR0/4EVR0-Server" \
      org.opencontainers.image.revision="${VCS_REF}"
# 답변별 버전 추적(release_versions)에 앱 커밋을 남긴다.
ENV APP_VCS_REF="${VCS_REF}"

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
