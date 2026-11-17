FROM alpine:3.22.2
RUN apk add --no-cache postgresql16-client rclone gnupg
COPY --chmod=755 infra/backup/backup.sh /usr/local/bin/soe-backup
ENTRYPOINT ["/usr/local/bin/soe-backup"]
