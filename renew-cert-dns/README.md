# Renew certificate by DNS

**Platform:** Linux

Issues or renews the Let's Encrypt certificate for `fringes.godswind.org` with a DNS TXT record. Certbot prints the record, this script waits until public DNS shows it, and then the certificate is saved. The copy in `/usr/local/bin` is what this machine runs. This directory is the source that survives a reinstall.

The certificate names `fringes.godswind.org`, `cameras.godswind.org`, and `games.godswind.org`. Each name needs its own TXT record during a run.
