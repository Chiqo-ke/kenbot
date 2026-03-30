"""Quick JWT round-trip diagnostic — delete after use."""
import sys, os
sys.path.insert(0, '.')
os.environ['DJANGO_SETTINGS_MODULE'] = 'kenbot.settings.development'

from dotenv import load_dotenv
load_dotenv('.env', override=False)

import django
django.setup()

from rest_framework_simplejwt.tokens import AccessToken
from django.contrib.auth import get_user_model
from django.conf import settings

print("SECRET_KEY (first 8):", settings.SECRET_KEY[:8])
print("SIMPLE_JWT:", settings.SIMPLE_JWT)
print()

User = get_user_model()
u = User.objects.first()
if not u:
    print("ERROR: No user in DB")
    sys.exit(1)

tok = AccessToken.for_user(u)
raw = str(tok)
print("Issued token for:", u.username)
print("Token exp:", tok["exp"])
print()

try:
    tok2 = AccessToken(raw)
    u2 = User.objects.get(pk=tok2["user_id"])
    print("Round-trip OK — user:", u2.username, "is_authenticated:", u2.is_authenticated)
except Exception as e:
    print("Round-trip FAILED:", e)
