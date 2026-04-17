# Z-Library Authentication Guide

## Getting Authentication Credentials

Z-Library requires authentication to download books. There are two methods to obtain credentials:

### Method 1: Extract from Browser Cookies (Recommended)

1. Visit Z-Library website and login with your account
2. Open browser developer tools (F12)
3. Go to "Application" or "Storage" tab
4. Find "Cookies" section
5. Look for these two cookies:
   - `remix_userid` - Your user ID (numeric)
   - `remix_userkey` - Your user key (alphanumeric string)

### Method 2: Login with Email and Password

Use the Zlibrary Python library to login once and extract the credentials:

```python
from Zlibrary import Zlibrary

Z = Zlibrary(email="your@email.com", password="yourpassword")
user_profile = Z.getProfile()["user"]

print("Remix User ID:", user_profile["id"])
print("Remix User Key:", user_profile["remix_userkey"])
```

Save these credentials for future use.

## Security Notes

- **Never share your credentials publicly**
- Store credentials securely (environment variables, config files with restricted permissions)
- Use `remix_userid` and `remix_userkey` instead of email/password for better security
- Credentials persist across sessions, no need to login repeatedly

## Download Limits

- Free accounts have daily download limits (typically 10 books per day)
- Premium accounts have higher limits
- Check remaining downloads with `getDownloadsLeft()` method
