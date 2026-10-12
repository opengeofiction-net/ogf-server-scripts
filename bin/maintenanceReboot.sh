#!/bin/bash
#
# Reboot this host if a package update has asked for it, from
# maintenance-reboot.timer inside the OGF maintenance window.
#
# unattended-upgrades installs the security updates and, when one of them is a
# kernel, libc, dbus or similar, writes /run/reboot-required. By itself it then
# only mails about it, so the host keeps running the old kernel until somebody
# does it by hand - which on a fleet this size means it does not happen.
#
# unattended-upgrades can reboot on its own (Automatic-Reboot), but only at a
# time of day, on whichever day the update landed. The announced window is one
# morning a week, so the decision lives here instead and that setting stays off.
#
# Guards, both of which postpone to the next slot rather than fail:
#   - a unit named in HOLD_UNITS is running: a backup or a long import must not
#     be cut in half. The timer has a second slot later in the window, so a job
#     that finishes inside the hour still gets the host rebooted that morning.
#     Name only jobs that end: a service that runs continuously, such as
#     tile-replicate@ogf-carto, would postpone the reboot for ever.
#   - somebody is logged in: they are probably in the middle of something, at an
#     hour when nobody is here by accident. A session left open all week
#     postpones the reboot all week - the unattended-upgrades mail keeps
#     arriving, and the journal here says who.
#
# /etc/opengeofiction/maintenance-reboot.env, read by the unit, both optional:
#   HOLD_UNITS="planet-backup.service danu-backup.service"
#   REBOOT_WITH_USERS=yes

set -euo pipefail

if [ ! -e /run/reboot-required ]; then
	echo "no reboot required"
	exit 0
fi

pkgs=$(sort -u /run/reboot-required.pkgs 2>/dev/null | tr '\n' ' ')
echo "reboot required by: ${pkgs:-unknown}"

for unit in ${HOLD_UNITS:-}; do
	if systemctl is-active --quiet "${unit}"; then
		echo "postponed: ${unit} is running"
		exit 0
	fi
done

if [ "${REBOOT_WITH_USERS:-no}" != "yes" ]; then
	users=$(who 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')
	if [ -n "${users}" ]; then
		echo "postponed: logged in: ${users}"
		exit 0
	fi
fi

echo "rebooting"
# --no-block: the reboot job is queued and this returns, so systemd is not
# waiting on a oneshot service that is waiting on the reboot
systemctl --no-block reboot
