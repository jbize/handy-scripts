#!/bin/bash
# Certificate Renewal Script with Automatic DNS Polling
# Works with Google Domains - automatically checks DNS propagation

DOMAIN="fringes.godswind.org"

echo "=========================================="
echo "Certificate Renewal - DNS Challenge"
echo "=========================================="
echo ""

# Create the polling hook script
sudo tee /etc/letsencrypt/dns-poll-hook.sh > /dev/null << 'HOOKEOF'
#!/bin/bash
# Certbot auth hook - automatically polls DNS for TXT record

CHALLENGE_NAME="_acme-challenge.$CERTBOT_DOMAIN"
TXT_VALUE="$CERTBOT_VALIDATION"

echo ""
echo "=========================================="
echo "DNS Challenge Information"
echo "=========================================="
echo "Please add this TXT record in Google Domains:"
echo ""
echo "  Name: $CHALLENGE_NAME"
echo "  Type: TXT"
echo "  Value: $TXT_VALUE"
echo ""
echo "Waiting for DNS propagation (checking every 5 seconds)..."
echo ""

# Poll DNS servers until record is visible
MAX_ATTEMPTS=60  # 5 minutes max
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    # Check multiple DNS servers for reliability
    RESULT1=$(dig +short TXT "$CHALLENGE_NAME" @8.8.8.8 2>/dev/null | grep -o "\"$TXT_VALUE\"")
    RESULT2=$(dig +short TXT "$CHALLENGE_NAME" @1.1.1.1 2>/dev/null | grep -o "\"$TXT_VALUE\"")
    RESULT3=$(dig +short TXT "$CHALLENGE_NAME" @208.67.222.222 2>/dev/null | grep -o "\"$TXT_VALUE\"")
    
    if [ -n "$RESULT1" ] || [ -n "$RESULT2" ] || [ -n "$RESULT3" ]; then
        echo "✓ DNS record is visible! Propagation confirmed."
        echo "Continuing with certificate issuance..."
        exit 0
    fi
    
    ATTEMPT=$((ATTEMPT + 1))
    if [ $((ATTEMPT % 6)) -eq 0 ]; then
        echo "  Still waiting... (checked ${ATTEMPT} times, ~$((ATTEMPT * 5)) seconds elapsed)"
    fi
    sleep 5
done

echo "⚠ Warning: DNS record not found after $MAX_ATTEMPTS attempts (~5 minutes)."
echo "The record may still be propagating. Certbot will also verify."
exit 0  # Continue anyway, certbot will do its own verification
HOOKEOF

# Create cleanup reminder hook
sudo tee /etc/letsencrypt/dns-cleanup-reminder.sh > /dev/null << 'CLEANUPEOF'
#!/bin/bash
# Certbot cleanup hook - reminds to delete TXT record
echo ""
echo "=========================================="
echo "IMPORTANT: Cleanup Reminder"
echo "=========================================="
echo "Please delete the TXT record from Google Domains:"
echo "  Name: _acme-challenge.$CERTBOT_DOMAIN"
echo ""
echo "The certificate has been issued and the TXT record is no longer needed."
CLEANUPEOF

# Make hooks executable
sudo chmod +x /etc/letsencrypt/dns-poll-hook.sh /etc/letsencrypt/dns-cleanup-reminder.sh

# Run certbot with automatic DNS polling
echo "Running certbot with automatic DNS polling..."
sudo certbot certonly --manual --preferred-challenges=dns \
    --manual-public-ip-logging-ok \
    -d "$DOMAIN" \
    --force-renewal \
    --manual-auth-hook /etc/letsencrypt/dns-poll-hook.sh \
    --manual-cleanup-hook /etc/letsencrypt/dns-cleanup-reminder.sh

if [ $? -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo "✓ Certificate renewed successfully!"
    echo "=========================================="
    
    # Copy to production location
    echo "Copying certificate to production location..."
    sudo cp -L /etc/letsencrypt/live/$DOMAIN/fullchain.pem /etc/fringes-adventure/ssl/fullchain.pem
    sudo cp -L /etc/letsencrypt/live/$DOMAIN/privkey.pem /etc/fringes-adventure/ssl/privkey.pem
    
    # Reload nginx
    echo "Reloading nginx..."
    sudo systemctl reload nginx
    
    # Update master certificates
    echo "Updating master certificates..."
    ~/certs/update_master_certs.sh
    
    echo ""
    echo "✓ Certificate deployed and nginx reloaded!"
    echo "✓ All done! Your site is now using the new certificate."
else
    echo ""
    echo "=========================================="
    echo "✗ Certificate renewal failed"
    echo "=========================================="
    exit 1
fi
