#!/usr/bin/env bash
set -euo pipefail

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_root="/opt/clahanlabs-backups/guacamole-${timestamp}"

echo "Creating backup directory: ${backup_root}"
sudo mkdir -p "$backup_root"

if [[ -d /etc/guacamole ]]; then
  echo "Backing up /etc/guacamole"
  sudo cp -a /etc/guacamole "${backup_root}/etc-guacamole"
fi

if [[ -d /var/lib/tomcat10/webapps ]]; then
  echo "Backing up Tomcat Guacamole webapps"
  sudo mkdir -p "${backup_root}/tomcat10-webapps"
  sudo cp -a /var/lib/tomcat10/webapps/guacamole* "${backup_root}/tomcat10-webapps/" 2>/dev/null || true
fi

for service in tomcat10 guacd; do
  if systemctl list-unit-files "${service}.service" >/dev/null 2>&1; then
    echo "Stopping and disabling ${service}.service"
    sudo systemctl stop "${service}" || true
    sudo systemctl disable "${service}" || true
  fi
done

echo
echo "Host Guacamole services disabled. Backup stored at:"
echo "$backup_root"
echo
echo "Ports after cleanup:"
sudo ss -ltnp | grep -E ':(8080|8090|4822)\b' || true
echo
echo "Packages were not purged. Remove them manually only after Docker Guacamole is confirmed working."
