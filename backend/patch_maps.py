"""
One-time script: add phase annotations, failure_subgoals, and requires_auth
to the ecitizen service map JSON files.
Run from: kenbot/backend/  with any Python 3.9+
"""
import json
from pathlib import Path

BASE = Path(__file__).parent / "map_files" / "ecitizen"


def save(name: str, data: dict) -> None:
    path = BASE / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  ✓ {name}")


# ─── ecitizen_login.json ──────────────────────────────────────────────────────
login = json.loads((BASE / "ecitizen_login.json").read_text(encoding="utf-8"))

phase_map_login = {
    "open_login_page": "Sign In",
    "enter_credentials": "Sign In",
    "login_success": "Sign In",
}
failure_subgoals_login = {
    "enter_credentials": [
        {"label": "Try again", "action": "retry", "service_id": None},
        {
            "label": "Forgot Password",
            "action": "sub_service",
            "service_id": "ecitizen_forgot_password",
        },
    ]
}

for step in login["workflow"]:
    sid = step["step_id"]
    if sid in phase_map_login:
        step["phase"] = phase_map_login[sid]
    if sid in failure_subgoals_login:
        step["failure_subgoals"] = failure_subgoals_login[sid]

save("ecitizen_login.json", login)


# ─── ecitizen_forgot_password.json ─────────────────────────────────────────────
fp = json.loads(
    (BASE / "ecitizen_forgot_password.json").read_text(encoding="utf-8")
)

phase_map_fp = {
    "open_forgot_password": "Reset Password",
    "select_account_type": "Reset Password",
    "enter_id_number": "Reset Password",
    "click_next_after_id": "Reset Password",
    "enter_otp": "Verify Identity",
    "set_new_password": "Set New Password",
    "password_reset_success": "Set New Password",
}

for step in fp["workflow"]:
    if step["step_id"] in phase_map_fp:
        step["phase"] = phase_map_fp[step["step_id"]]

save("ecitizen_forgot_password.json", fp)


# ─── apply_driving_licence.json ─────────────────────────────────────────────
adl = json.loads(
    (BASE / "apply_driving_licence.json").read_text(encoding="utf-8")
)

phase_map_adl = {
    "open_ntsa_portal": "Open Portal",
    "ntsa_dashboard": "Navigate",
    "select_new_application": "Navigate",
    "fill_personal_details": "Fill Details",
    "select_licence_class": "Fill Details",
    "upload_documents": "Upload Documents",
    "review_and_submit": "Review & Submit",
    "mpesa_payment": "Payment",
}

for step in adl["workflow"]:
    if step["step_id"] in phase_map_adl:
        step["phase"] = phase_map_adl[step["step_id"]]

save("apply_driving_licence.json", adl)


# ─── renew_driving_license.json ─────────────────────────────────────────────
rdl = json.loads(
    (BASE / "renew_driving_license.json").read_text(encoding="utf-8")
)

# 1. Remove inlined ecitizen_login step
rdl["workflow"] = [
    s for s in rdl["workflow"] if s["step_id"] != "ecitizen_login"
]
# 2. Remove ecitizen_email / ecitizen_password from required_user_data
rdl["required_user_data"] = [
    k for k in rdl["required_user_data"]
    if k not in ("ecitizen_email", "ecitizen_password")
]
# 3. Add requires_auth
rdl["requires_auth"] = "ecitizen_login"

phase_map_rdl = {
    "navigate_ntsa_renewal": "Navigate",
    "fill_licence_details": "Fill Details",
    "confirm_and_pay": "Payment",
}
failure_subgoals_rdl = {
    "fill_licence_details": [
        {"label": "Try again", "action": "retry", "service_id": None},
    ]
}

for step in rdl["workflow"]:
    sid = step["step_id"]
    if sid in phase_map_rdl:
        step["phase"] = phase_map_rdl[sid]
    if sid in failure_subgoals_rdl:
        step["failure_subgoals"] = failure_subgoals_rdl[sid]

save("renew_driving_license.json", rdl)


# ─── good_conduct_certificate.json ──────────────────────────────────────────
gcc = json.loads(
    (BASE / "good_conduct_certificate.json").read_text(encoding="utf-8")
)

# 1. Remove inlined ecitizen_login step
gcc["workflow"] = [
    s for s in gcc["workflow"] if s["step_id"] != "ecitizen_login"
]
# 2. Remove ecitizen_email / ecitizen_password from required_user_data
gcc["required_user_data"] = [
    k for k in gcc["required_user_data"]
    if k not in ("ecitizen_email", "ecitizen_password")
]
# 3. Add requires_auth
gcc["requires_auth"] = "ecitizen_login"

phase_map_gcc = {
    "navigate_dci_good_conduct": "Navigate",
    "fill_personal_details": "Fill Details",
    "confirm_and_pay": "Payment",
}

for step in gcc["workflow"]:
    sid = step["step_id"]
    if sid in phase_map_gcc:
        step["phase"] = phase_map_gcc[sid]

save("good_conduct_certificate.json", gcc)

print("\nAll map files patched successfully.")
