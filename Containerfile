FROM python:3.12-alpine

RUN addgroup -S fmtelegram && adduser -S -G fmtelegram -u 10001 fmtelegram
WORKDIR /app
COPY --chown=fmtelegram:fmtelegram fm_telegram.py oss_watch.py /app/
USER 10001:10001
ENV FM_TELEGRAM_CONFIG=/config/config.json \
    FM_TELEGRAM_STATE_DIR=/state \
    FM_HOME=/firstmate \
    PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "/app/fm_telegram.py", "run"]
