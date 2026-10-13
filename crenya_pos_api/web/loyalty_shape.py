"""A loyalty program as app.crenya.com edits it (pure: no site): ERPNext's own Loyalty Program,
which the tills read (`sync/loyalty.py`: earning on every sale to an enrolled customer, the
balance, redemption). Its fields keep ERPNext's names; decimals are strings.

LoyaltyProgram: {name?, loyalty_program_name, loyalty_program_type, from_date, to_date?,
customer_group?, customer_territory?, auto_opt_in, conversion_factor, expiry_duration,
expense_account, cost_center, collection_rules: [{tier_name, min_spent, collection_factor}],
modified?}; the answer adds `active` (today within its dates).
"""

from datetime import date

from crenya_pos_api.utils.decimal import DecimalParseError, as_decimal, parse_decimal

MAX_NAME_LENGTH = 140
# ERPNext's program type with one tier (its other type has several)
SINGLE_TIER = "Single Tier Program"
TIER_KEYS = ("tier_name", "min_spent", "collection_factor")
KEYS = (
	"name",
	"loyalty_program_name",
	"loyalty_program_type",
	"from_date",
	"to_date",
	"customer_group",
	"customer_territory",
	"auto_opt_in",
	"conversion_factor",
	"expiry_duration",
	"expense_account",
	"cost_center",
	"collection_rules",
	"modified",
)
# the fields written to the Loyalty Program as sent (the tiers apart)
PLAIN_FIELDS = (
	"loyalty_program_type",
	"from_date",
	"to_date",
	"customer_group",
	"customer_territory",
	"expense_account",
	"cost_center",
)


def _date(value, field, required=False):
	if value in (None, ""):
		if required:
			raise ValueError(f"{field} is required")
		return None
	if not isinstance(value, str):
		raise ValueError(f"{field} must be YYYY-MM-DD")
	try:
		return date.fromisoformat(value)
	except ValueError:
		raise ValueError(f"{field} must be YYYY-MM-DD")


def _decimals(value) -> int:
	return max(-value.as_tuple().exponent, 0)


def _number(value) -> float:
	"""A checked decimal string as the float the Loyalty Program field holds (ERPNext compares
	them as numbers: its lowest tier check reads a string "0" as not 0)."""
	return float(as_decimal(value))


def _text(value):
	return value.strip() if isinstance(value, str) else ""


def loyalty_problem(data, precision, program_types) -> str | None:
	"""Why `data` is not a loyalty program to save, or None. `precision`: the decimals of the
	outlet's currency (a tier's minimum spent); `program_types`: ERPNext's program type options."""
	if not isinstance(data, dict):
		return "A loyalty program must be an object"
	unknown = sorted(set(data) - set(KEYS))
	if unknown:
		return f"Not a loyalty program field: {', '.join(unknown)}"
	name = _text(data.get("loyalty_program_name"))
	if not name or len(name) > MAX_NAME_LENGTH:
		return f"A name of at most {MAX_NAME_LENGTH} characters is required"
	if data.get("name") and data["name"] != name:
		return "A loyalty program cannot be renamed"
	if data.get("loyalty_program_type") not in program_types:
		return f"loyalty_program_type must be one of {', '.join(program_types)}"
	for field in ("expense_account", "cost_center"):
		if not _text(data.get(field)):
			return f"{field} is required (points are redeemed against it)"
	for field in ("customer_group", "customer_territory"):
		if data.get(field) not in (None, "") and not _text(data.get(field)):
			return f"{field} must be a name"
	expiry = data.get("expiry_duration")
	if expiry not in (None, "") and (isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < 0):
		return "expiry_duration must be a whole number of days, 0 or more"
	if data.get("auto_opt_in") not in (None, True, False, 0, 1):
		return "auto_opt_in must be true or false"
	try:
		from_date = _date(data.get("from_date"), "from_date", required=True)
		to_date = _date(data.get("to_date"), "to_date")
		if parse_decimal(data.get("conversion_factor"), "conversion_factor") <= 0:
			return "conversion_factor (the value of one point) must be more than 0"
		problem = _tiers_problem(data.get("collection_rules"), precision, data["loyalty_program_type"])
	except (DecimalParseError, ValueError) as e:
		return str(e)
	if problem:
		return problem
	if to_date and to_date < from_date:
		return "to_date cannot be before from_date"
	return None


def _tiers_problem(tiers, precision, program_type):
	if not isinstance(tiers, list) or not tiers:
		return "Add at least one tier"
	if program_type == SINGLE_TIER and len(tiers) != 1:
		return "A single tier program has exactly one tier"
	names = set()
	lowest = None
	for idx, tier in enumerate(tiers, start=1):
		if not isinstance(tier, dict) or set(tier) - set(TIER_KEYS):
			return f"Tier {idx} must be an object of {', '.join(TIER_KEYS)}"
		tier_name = _text(tier.get("tier_name"))
		if not tier_name:
			return f"Tier {idx} needs a name"
		if tier_name in names:
			return f"Tier {tier_name} is listed twice"
		names.add(tier_name)
		min_spent = parse_decimal(tier.get("min_spent") or "0", f"tier {idx} min_spent")
		if min_spent < 0 or _decimals(min_spent) > precision:
			return f"Tier {idx}: the minimum spent must be 0 or more, with at most {precision} decimals"
		if parse_decimal(tier.get("collection_factor"), f"tier {idx} collection_factor") <= 0:
			return f"Tier {idx}: the amount spent per point must be more than 0"
		lowest = min_spent if lowest is None else min(lowest, min_spent)
	if lowest != 0:
		return "The lowest tier must have a minimum spent of 0: every enrolled customer is in a tier"
	return None


def to_loyalty_program(data) -> dict:
	"""The Loyalty Program fields and tiers of a checked program (`loyalty_problem` None); the
	caller adds the company."""
	program = {field: (data.get(field) or None) for field in PLAIN_FIELDS}
	program.update(
		{
			"loyalty_program_name": _text(data["loyalty_program_name"]),
			"auto_opt_in": 1 if data.get("auto_opt_in") else 0,
			"conversion_factor": _number(data["conversion_factor"]),
			"expiry_duration": int(data.get("expiry_duration") or 0),
			"collection_rules": [
				{
					"tier_name": _text(tier["tier_name"]),
					"min_spent": _number(tier.get("min_spent")),
					"collection_factor": _number(tier["collection_factor"]),
				}
				for tier in data["collection_rules"]
			],
		}
	)
	return program
