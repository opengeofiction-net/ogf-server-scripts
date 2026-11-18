#!/bin/bash
#
# Back up the Forgejo instance (git.opengeofiction.net) with `forgejo dump`,
# and queue the archive for AWS S3 as <timeframe>:git:backup-<date>-forgejo.tar.gz
#
# Run as the Forgejo user (git) with ogf as a supplementary group, from
# ogfutil-backupGit.service. The dump holds the SQLite database as SQL,
# the repositories, data (avatars, attachments, LFS, packages) and app.ini.
# Restore is in Admin:Creating a git server.

set -euo pipefail

if [ $# -ne 1 ]; then
	cat <<USAGE
Usage:
	$0 dir
USAGE
	exit 1
fi
DESTDIR=$1                                   # /opt/opengeofiction/backup-git
FORGEJO=${FORGEJO:-/usr/local/bin/forgejo}
FORGEJO_CONFIG=${FORGEJO_CONFIG:-/etc/forgejo/app.ini}
FORGEJO_WORK=${FORGEJO_WORK:-/var/lib/forgejo}
BACKUP_QUEUE=/opt/opengeofiction/backup-to-s3-queue
KEEP_DAYS=7
DATESTR=$(date '+%Y%m%d%H%M')
NAME=backup-${DATESTR}-forgejo.tar.gz

echo "Started: $(date '+%Y%m%d%H%M%S')"

# daily / weekly / monthly / yearly - the same rule as backupWiki.sh and
# backupPlanet.sh, which the S3 retention relies on
lastthu=$(ncal -h | awk '/Th/ {print $NF}')
today=$(date +%-d)
timeframe=daily
if [[ $(date +%u) -eq 4 ]]; then
	timeframe=weekly
	if [[ $lastthu -eq $today ]]; then
		timeframe=monthly
		if [[ $(date +%-m) -eq 12 ]]; then
			timeframe=yearly
		fi
	fi
fi
echo "Timeframe: ${timeframe}"

for dir in "$DESTDIR" "$BACKUP_QUEUE"; do
	if [ ! -w "$dir/" ]; then
		echo "ERROR: $dir does not exist or not writable" >&2
		exit 2
	fi
done

echo "Cleaning up"
find "$DESTDIR" -maxdepth 1 -name 'backup-*-forgejo.tar.gz' -mtime +$KEEP_DAYS -print -delete
find "$DESTDIR" -maxdepth 1 -name '.part-*' -print -delete

# dump to a temporary name, so nothing half-written is ever queued. It must
# end .tar.gz, or forgejo dump appends that
PART=$DESTDIR/.part-$NAME
echo "Forgejo dump"
"$FORGEJO" dump --config "$FORGEJO_CONFIG" --work-path "$FORGEJO_WORK" \
	--type tar.gz --file "$PART" \
	--skip-log --skip-index --skip-repo-archives --quiet

# sanity check: readable, and holds the database and app.ini
listing=$(tar -tzf "$PART")
for want in forgejo-db.sql app.ini; do
	if ! grep -q "$want\$" <<<"$listing"; then
		echo "ERROR: dump has no $want" >&2
		exit 3
	fi
done
# forgejo dump writes 0600; ogf uploads it, through the group
chmod 0640 "$PART"
mv "$PART" "$DESTDIR/$NAME"

echo "Queue for S3"
ln "$DESTDIR/$NAME" "${BACKUP_QUEUE}/${timeframe}:git:${NAME}"

echo "Backups:"
ls -lh "$DESTDIR"

echo "Finished: $(date '+%Y%m%d%H%M%S')"
