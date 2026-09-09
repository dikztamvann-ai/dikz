#!/usr/bin/env python3
"""
Test script for amprem.irfanjawa.com API flow
Full flow: register -> login -> send-magic-link -> verify-magic-link -> ads -> apply
"""
import httpx
import random
import string
import time
import re
from datetime import datetime

BASE = "https://amprem.irfanjawa.com"
TURNSTILE_SOLVER = "https://kyzznekoo.zone.id/api/cloudflare/turnstileMin"
TURNSTILE_SITEKEY = "0x4AAAAAADsWLA16vNVNqTCH"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def random_email():
    """Generate random test email"""
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"test{rand}@bingung.dev"

def random_password():
    """Generate random password (8+ chars, uppercase, number, special)"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=10)) + "A1!"

async def solve_turnstile():
    """Solve Turnstile challenge via external service"""
    log("Solving Turnstile...")
    try:
        async with httpx.AsyncClient(timeout=50.0) as client:
            resp = await client.get(
                TURNSTILE_SOLVER,
                params={
                    "url": f"{BASE}/auth",
                    "sitekey": TURNSTILE_SITEKEY
                }
            )
            data = resp.json()
            token = data.get("data", {}).get("token")
            if not token:
                raise Exception(f"Turnstile solve failed: {data}")
            log(f"✓ Turnstile solved ({len(token)} chars)")
            return token
    except Exception as e:
        log(f"✗ Turnstile error: {e}")
        raise

async def register(client, email, password):
    """POST /api/auth/register"""
    log(f"Registering {email}...")
    token = await solve_turnstile()

    resp = await client.post(
        f"{BASE}/api/auth/register",
        json={
            "email": email,
            "password": password,
            "turnstileToken": token
        },
        headers={
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/auth?tab=register"
        }
    )

    if resp.status_code >= 400:
        raise Exception(f"Register failed {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if not data.get("success"):
        raise Exception(f"Register not success: {data}")

    log(f"✓ Registered")
    return data

async def login(client, email, password):
    """POST /api/auth/login -> returns session cookie"""
    log(f"Logging in {email}...")
    token = await solve_turnstile()

    resp = await client.post(
        f"{BASE}/api/auth/login",
        json={
            "email": email,
            "password": password,
            "turnstileToken": token
        },
        headers={
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/auth?tab=register"
        }
    )

    if resp.status_code >= 400:
        raise Exception(f"Login failed {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if not data.get("success"):
        raise Exception(f"Login not success: {data}")

    # Extract session cookie
    session_cookie = None
    for cookie_header in resp.headers.get_list("set-cookie"):
        if cookie_header.startswith("session="):
            session_cookie = cookie_header.split(";")[0]
            break

    if not session_cookie:
        raise Exception("No session cookie in login response")

    log(f"✓ Logged in, session: {session_cookie[:50]}...")
    return session_cookie, data

async def send_magic_link(client, session_cookie, email):
    """POST /api/auth/send-magic-link"""
    log(f"Sending magic link to {email}...")

    resp = await client.post(
        f"{BASE}/api/auth/send-magic-link",
        json={"email": email},
        headers={
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/dashboard/generator",
            "Cookie": session_cookie
        }
    )

    if resp.status_code >= 400:
        raise Exception(f"Send magic link failed {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if not data.get("success"):
        raise Exception(f"Send magic link not success: {data}")

    log(f"✓ Magic link sent: {data.get('message')}")
    return data

async def verify_magic_link(client, session_cookie, email, magic_link):
    """POST /api/auth/verify-magic-link"""
    log(f"Verifying magic link...")

    resp = await client.post(
        f"{BASE}/api/auth/verify-magic-link",
        json={
            "email": email,
            "magicLink": magic_link
        },
        headers={
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/dashboard/generator",
            "Cookie": session_cookie
        }
    )

    if resp.status_code >= 400:
        raise Exception(f"Verify magic link failed {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if not data.get("success"):
        raise Exception(f"Verify magic link not success: {data}")

    log(f"✓ Verified: {data.get('message')}")
    return data

async def record_ads(client, cookies):
    """POST /api/ads/record repeatedly until count >= 5"""
    log("Recording ad views...")
    count = 0

    for i in range(1, 11):
        resp = await client.post(
            f"{BASE}/api/ads/record",
            headers={
                "Content-Type": "application/json",
                "Origin": BASE,
                "Referer": f"{BASE}/dashboard/generator",
                "Cookie": cookies
            }
        )

        data = resp.json()
        log(f"  #{i} -> {resp.status_code}: {data}")

        # Update cookies if new ads_session is set
        for cookie_header in resp.headers.get_list("set-cookie"):
            if cookie_header.startswith("ads_session="):
                new_cookie = cookie_header.split(";")[0]
                cookies = f"{cookies}; {new_cookie}"

        if resp.status_code == 200 and data.get("success"):
            count = data.get("count", count)
            if count >= 5:
                log(f"✓ Ad count reached {count}")
                return cookies, count
        elif resp.status_code == 400:
            # Rate limit or cooldown
            error = data.get("error", "")
            wait_match = re.search(r'(\d+)\s*detik', error, re.I)
            if wait_match:
                wait_sec = int(wait_match.group(1)) + 1
                log(f"  Cooldown {wait_sec}s, waiting...")
                time.sleep(wait_sec)
                continue
            else:
                log(f"  Error: {error}")

        # Small delay between attempts
        time.sleep(2)

    log(f"✓ Ad recording done, final count: {count}")
    return cookies, count

async def apply_generator(client, cookies):
    """POST /api/generator/apply -> activate premium"""
    log("Applying generator (activate premium)...")

    resp = await client.post(
        f"{BASE}/api/generator/apply",
        headers={
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/dashboard/generator-v2",
            "Cookie": cookies
        }
    )

    if resp.status_code >= 400:
        raise Exception(f"Generator apply failed {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    log(f"✓ Generator apply result: {data}")
    return data

async def main():
    """Test full flow"""
    email = input("Enter target email (or press Enter for random temp): ").strip()
    if not email:
        email = random_email()
        log(f"Using random email: {email}")

    password = random_password()
    log(f"Using password: {password}")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        # Step 1: Register
        try:
            await register(client, email, password)
        except Exception as e:
            log(f"Register failed (might already exist): {e}")

        # Step 2: Login
        session_cookie, user = await login(client, email, password)

        # Step 3: Send magic link
        await send_magic_link(client, session_cookie, email)

        # Step 4: Wait for user to paste magic link
        print("\n" + "="*70)
        print("MANUAL STEP REQUIRED:")
        print(f"1. Check email inbox: {email}")
        print("2. Find email from 'Alight Motion' / 'Alight Creative'")
        print("3. Long-press the login button -> Copy URL (don't click it)")
        print("4. Paste the full URL below")
        print("="*70)
        magic_link = input("\nPaste magic link URL: ").strip()

        if not magic_link:
            log("✗ No magic link provided, stopping.")
            return

        # Step 5: Verify magic link
        await verify_magic_link(client, session_cookie, email, magic_link)

        # Step 6: Record ads
        final_cookies, ad_count = await record_ads(client, session_cookie)

        if ad_count < 5:
            log(f"✗ Ad count {ad_count} < 5, cannot activate premium")
            return

        # Step 7: Apply generator (activate premium)
        result = await apply_generator(client, final_cookies)

        print("\n" + "="*70)
        print("DONE!")
        print(f"Email: {email}")
        print(f"Password: {password}")
        print(f"Result: {result}")
        print("="*70)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
