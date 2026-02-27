FROM alpine:3.19

RUN apk add --no-cache \
    bash \
    git \
    postgresql-client

COPY backup.sh /backup.sh
COPY run.sh /run.sh
RUN chmod +x /backup.sh /run.sh

# Loop-based scheduler (avoids cron setpgid: Operation not permitted in containers)
# Default: every 6 hours. Override with BACKUP_INTERVAL_SECONDS.
ENV BACKUP_INTERVAL_SECONDS=21600
CMD ["/run.sh"]