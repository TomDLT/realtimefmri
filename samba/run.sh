#!/usr/bin/env bash
SAMBA_USER=rtfmri
SAMBA_PASSWORD=password
echo -ne "$SAMBA_PASSWORD\n$SAMBA_PASSWORD\n" | smbpasswd -a -s $SAMBA_USER
service smbd start
tail -F /var/log/samba/log.smbd
