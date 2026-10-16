# Renew certificate by DNS

**Platform:** Linux

Issues or renews the Let's Encrypt certificate for `fringes.godswind.org` with a DNS TXT record. Certbot prints the record, this script waits until public DNS shows it, and then the certificate is saved. The copy in `/usr/local/bin` is what this machine runs. This directory is the source that survives a reinstall.

The script names one domain today. A certificate that also covers `cameras.godswind.org` and `games.godswind.org`, or a wildcard, is a change to that name list and a TXT record for each name.
