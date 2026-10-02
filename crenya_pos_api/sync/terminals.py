"""Payment terminals a till can drive (UPI QR, Pine Labs Cloud, card ECRs).

A **Crenya POS Payment Terminal** belongs to a company and optionally to one POS
Profile (blank = every profile of the company). Bootstrap sends a registered
device the enabled terminals of its profile whose mode of payment is on the
profile, with the provider's own settings only; the Pine Labs security token is
decrypted for that bootstrap and nowhere else.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint

TERMINAL_DOCTYPE = "Crenya POS Payment Terminal"
SECRET_FIELD = "security_token"

# Select label (desk) -> provider key (protocol)
UPI_QR = "UPI QR"
PINE_LABS_CLOUD = "Pine Labs Cloud"
GEIDEA = "Geidea"
NETWORK_ECR = "Network International ECR"
BANK_ECR = "Bank ECR"
PROVIDERS = {
	UPI_QR: "upi_qr",
	PINE_LABS_CLOUD: "pinelabs_cloud",
	GEIDEA: "geidea",
	NETWORK_ECR: "network_ecr",
	BANK_ECR: "bank_ecr",
}
ECR_PROVIDERS = (GEIDEA, NETWORK_ECR, BANK_ECR)
# cloud APIs need the internet at payment time; a UPI QR is built offline and a bank ECR
# is reached over the shop's LAN
NEEDS_INTERNET = {
	"upi_qr": False,
	"pinelabs_cloud": True,
	"geidea": True,
	"network_ecr": True,
	"bank_ecr": False,
}

ENVIRONMENTS = ("UAT", "Production")
DEFAULT_ENVIRONMENT = "UAT"

# fieldname -> label of the identifiers (single words)
PINE_LABS_IDS = {
	"merchant_id": "Merchant ID",
	"store_id": "Store ID",
	"client_id": "Client ID",
	"user_id": "User ID",
}
PINE_LABS_REQUIRED = ("merchant_id", "store_id", "client_id")
ECR_IDS = {"host": "Host", "terminal_id": "Terminal ID"}
MAX_ID_LENGTH = 140
MAX_AUTO_CANCEL_MINUTES = 1440
MAX_PORT = 65535

# NPCI virtual payment address: user part (letters, digits, . _ -) @ the PSP handle
_VPA_RE = re.compile(r"^[A-Za-z0-9._-]{2,256}@[A-Za-z][A-Za-z0-9.-]{1,63}$")
_ID_RE = re.compile(r"^\S+$")
_PAYMENT_MODE_RE = re.compile(r"^\d+(,\d+)*$")

# fields read for the bootstrap, besides the provider settings
_BASE_FIELDS = ["name", "provider", "mode_of_payment", "label", "pos_profile"]
_SETTINGS_FIELDS = [
	"upi_vpa",
	"upi_payee_name",
	"environment",
	"merchant_id",
	"store_id",
	"client_id",
	"user_id",
	"allowed_payment_mode",
	"auto_cancel_minutes",
	"host",
	"port",
	"terminal_id",
]


def provider_key(provider):
	return PROVIDERS.get(provider)


def is_valid_vpa(value):
	return bool(isinstance(value, str) and _VPA_RE.match(value))


def _text(value):
	if value is None:
		return None
	value = str(value).strip()
	return value or None


def _strip_fields(doc, fieldnames):
	for fieldname in fieldnames:
		setattr(doc, fieldname, _text(doc.get(fieldname)))


def validate_settings(doc):
	"""Provider settings of a terminal (no database access); normalizes text fields in place."""
	_strip_fields(
		doc, ("label", "upi_vpa", "upi_payee_name", "allowed_payment_mode", *PINE_LABS_IDS, *ECR_IDS)
	)
	if not doc.get("label"):
		frappe.throw(_("Label is required"))
	provider = doc.get("provider")
	if provider not in PROVIDERS:
		frappe.throw(_("Provider must be one of {0}").format(", ".join(PROVIDERS)))

	if provider == UPI_QR:
		_validate_upi(doc)
	elif provider == PINE_LABS_CLOUD:
		_validate_pine_labs(doc)
	else:
		_validate_ecr(doc)


def _validate_upi(doc):
	vpa = doc.get("upi_vpa")
	if not vpa:
		frappe.throw(_("UPI ID (VPA) is required for a UPI QR terminal"))
	if not is_valid_vpa(vpa):
		frappe.throw(
			_("UPI ID {0} is not valid: use the form name@bank, for example shop@okhdfc").format(vpa)
		)


def _validate_pine_labs(doc):
	if doc.get("environment") not in ENVIRONMENTS:
		doc.environment = DEFAULT_ENVIRONMENT
	for fieldname in PINE_LABS_REQUIRED:
		if not doc.get(fieldname):
			frappe.throw(_("{0} is required for a Pine Labs Cloud terminal").format(PINE_LABS_IDS[fieldname]))
	_validate_ids(doc, PINE_LABS_IDS)
	if not doc.get(SECRET_FIELD):
		frappe.throw(_("Security Token is required for a Pine Labs Cloud terminal"))
	mode = doc.get("allowed_payment_mode")
	if mode and not _PAYMENT_MODE_RE.match(mode):
		frappe.throw(_("Allowed Payment Mode must be the Pine Labs payment mode code, for example 1 or 1,10"))
	minutes = cint(doc.get("auto_cancel_minutes"))
	if minutes < 0 or minutes > MAX_AUTO_CANCEL_MINUTES:
		frappe.throw(_("Auto Cancel (Minutes) must be between 0 and {0}").format(MAX_AUTO_CANCEL_MINUTES))


def _validate_ecr(doc):
	port = cint(doc.get("port"))
	if port < 0 or port > MAX_PORT:
		frappe.throw(_("Port must be between 1 and {0}").format(MAX_PORT))
	_validate_ids(doc, ECR_IDS)


def _validate_ids(doc, labels):
	for fieldname, label in labels.items():
		value = doc.get(fieldname)
		if value and (len(value) > MAX_ID_LENGTH or not _ID_RE.match(value)):
			frappe.throw(
				_("{0} must be a single word of at most {1} characters").format(label, MAX_ID_LENGTH)
			)


def profile_payment_modes(profile):
	return [row.mode_of_payment for row in profile.get("payments") or [] if row.mode_of_payment]


def mode_has_company_account(mode_of_payment, company):
	return bool(
		frappe.db.exists(
			"Mode of Payment Account",
			{"parenttype": "Mode of Payment", "parent": mode_of_payment, "company": company},
		)
	)


def validate_links(doc):
	"""Mode of payment of the company and, with a POS Profile, of that profile."""
	company = doc.get("company")
	mode = doc.get("mode_of_payment")
	if not mode_has_company_account(mode, company):
		frappe.throw(
			_("Mode of Payment {0} has no account for company {1}: add it in the Mode of Payment").format(
				mode, company
			)
		)
	pos_profile = doc.get("pos_profile")
	if not pos_profile:
		return
	profile = frappe.get_cached_doc("POS Profile", pos_profile)
	if profile.company != company:
		frappe.throw(
			_("POS Profile {0} belongs to company {1}, not {2}").format(pos_profile, profile.company, company)
		)
	if mode not in profile_payment_modes(profile):
		frappe.throw(
			_("Mode of Payment {0} is not in the payment methods of POS Profile {1}").format(
				mode, pos_profile
			)
		)


def _int_or_none(value):
	value = cint(value)
	return value or None


def provider_config(provider, row, secret=None):
	"""Settings a till needs for one terminal, only the provider's own (pure)."""
	if provider == "upi_qr":
		return {"upi_vpa": _text(row.get("upi_vpa")), "upi_payee_name": _text(row.get("upi_payee_name"))}
	if provider == "pinelabs_cloud":
		environment = (
			row.get("environment") if row.get("environment") in ENVIRONMENTS else DEFAULT_ENVIRONMENT
		)
		return {
			"environment": environment.lower(),
			"merchant_id": _text(row.get("merchant_id")),
			"store_id": _text(row.get("store_id")),
			"client_id": _text(row.get("client_id")),
			"user_id": _text(row.get("user_id")),
			"security_token": secret or None,
			"allowed_payment_mode": _text(row.get("allowed_payment_mode")),
			"auto_cancel_minutes": _int_or_none(row.get("auto_cancel_minutes")),
		}
	return {
		"host": _text(row.get("host")),
		"port": _int_or_none(row.get("port")),
		"terminal_id": _text(row.get("terminal_id")),
	}


def format_terminals(rows, profile_name, payment_modes, get_secret):
	"""Bootstrap `payment_terminals` (pure): terminals of the profile (or of every profile) whose
	mode of payment the profile offers. `get_secret(name)` is only called for Pine Labs rows."""
	modes = set(payment_modes)
	result = []
	for row in rows:
		provider = provider_key(row.get("provider"))
		if not provider:
			continue
		if row.get("pos_profile") and row.get("pos_profile") != profile_name:
			continue
		if row.get("mode_of_payment") not in modes:
			continue
		secret = get_secret(row["name"]) if provider == "pinelabs_cloud" else None
		result.append(
			{
				"name": row["name"],
				"provider": provider,
				"mode_of_payment": row.get("mode_of_payment"),
				"label": _text(row.get("label")) or row["name"],
				"needs_internet": NEEDS_INTERNET[provider],
				"config": provider_config(provider, row, secret),
			}
		)
	return result


def _decrypted_secret(name):
	from frappe.utils.password import get_decrypted_password

	return get_decrypted_password(TERMINAL_DOCTYPE, name, SECRET_FIELD, raise_exception=False)


def payment_terminals(profile):
	"""Terminals for a registered device's bootstrap (the caller has authorized the device)."""
	rows = frappe.get_all(
		TERMINAL_DOCTYPE,
		filters={"enabled": 1, "company": profile.company},
		fields=_BASE_FIELDS + _SETTINGS_FIELDS,
		order_by="label asc, name asc",
	)
	return format_terminals(rows, profile.name, profile_payment_modes(profile), _decrypted_secret)
