"""The cash over/short of a till's shift as a Journal Entry (0.17.0), an outlet's choice.

When the outlet's POS Profile switch `crenya_post_cash_difference` is on, a pushed shift whose
Cash-type modes of payment counted a difference posts one submitted Journal Entry at the business
date of the shift's close (the site's time zone, as `closed_at` is stored):

- short (counted < expected): Dr the outlet's over/short account, Cr the mode's cash account;
- over: the reverse.

The cash account is the Mode of Payment Account's default account for the company; the cost center
the outlet's, else the company's default. A card difference is never posted: it is a reconciliation
problem, not cash in the drawer.

Never refuse what the till did: the shift is recorded whatever happens here. A setup problem (the
switch on and no account, an account of another company or a group, a mode without an account, a
frozen date) or ERPNext refusing the entry leaves `cash_difference_state` "Not posted" with the
words in `cash_difference_error`; the shop's manager presses Retry on app.crenya.com after the
owner fixes it (`api/web_admin/day_close.post_cash_difference`). The states:

- "Posted": the entry is `cash_difference_entry`;
- "Not posted": `cash_difference_error` says why ("<code>: <words>"; the Z answer sends the code);
- "Not needed": no Cash-type mode has a difference;
- "Off": the outlet's switch was off when the shift arrived.

One entry per shift: the Journal Entry carries `crenya_pos_shift`, and a post looks for an existing
entry of the shift (not cancelled) before making one. A Retry takes the database lock
"crenya_pos_cash_difference:<shift>" first: under MariaDB's REPEATABLE READ a row lock then a count is
stale, a named lock taken before the transaction's first read is not.
"""

import re
from contextlib import contextmanager
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from crenya_pos_api.setup.install import CASH_DIFFERENCE_ACCOUNT_FIELD, CASH_DIFFERENCE_SWITCH_FIELD
from crenya_pos_api.sync.context import DeviceContext, money_precision
from crenya_pos_api.sync.errors import clean_message
from crenya_pos_api.utils.decimal import as_decimal, format_money, quantize
from crenya_pos_api.utils.session import elevated_as_administrator

SHIFT_DOCTYPE = "Crenya POS Shift"
JOURNAL_ENTRY = "Journal Entry"
SHIFT_LINK_FIELD = "crenya_pos_shift"
POSTED = "Posted"
NOT_POSTED = "Not posted"
NOT_NEEDED = "Not needed"
OFF = "Off"
STATES = (POSTED, NOT_POSTED, NOT_NEEDED, OFF)
ACCOUNT_ROOT_TYPES = ("Expense", "Income")
LOCK_PREFIX = "crenya_pos_cash_difference:"
LOCK_WAIT_SECONDS = 25
SAVEPOINT = "crenya_cash_difference"


# the codes a "Not posted" shift's `error` carries in the Z answer (the web translates them); the
# words for the desk and the Sync Event note follow the code in `cash_difference_error`
MISSING_ACCOUNT = "missing_account"
ACCOUNT_NOT_LEAF = "account_not_leaf"
ACCOUNT_WRONG_TYPE = "account_wrong_type"
NO_CASH_ACCOUNT = "no_cash_account"
FROZEN_DATE = "frozen_date"
# code-shaped, shown by the web as one generic line
ACCOUNT_WRONG_COMPANY = "account_wrong_company"
ACCOUNT_DISABLED = "account_disabled"
NO_COST_CENTER = "no_cost_center"
ENTRY_REFUSED = "entry_refused"
POSTING_FAILED = "posting_failed"
ERROR_CODES = (
	MISSING_ACCOUNT,
	ACCOUNT_NOT_LEAF,
	ACCOUNT_WRONG_TYPE,
	NO_CASH_ACCOUNT,
	FROZEN_DATE,
	ACCOUNT_WRONG_COMPANY,
	ACCOUNT_DISABLED,
	NO_COST_CENTER,
	ENTRY_REFUSED,
	POSTING_FAILED,
)


class NotPostable(Exception):
	"""A setup problem: the shift's entry cannot be posted until the owner fixes it (`code`, and the
	words as the message)."""

	def __init__(self, code, message):
		super().__init__(message)
		self.code = code


def stored_error(code, words) -> str:
	"""`cash_difference_error` as stored: "<code>: <words>" (the Z answer sends the code alone)."""
	return f"{code}: {clean_message(words)}" if words else code


def error_code(stored) -> str | None:
	"""The code of a stored `cash_difference_error` (pure); anything not code-shaped is POSTING_FAILED."""
	if not stored:
		return None
	code = str(stored).split(":", 1)[0].strip()
	return code if re.fullmatch(r"[a-z][a-z_]{0,39}", code) else POSTING_FAILED


def cash_rows(shift_doc, cash_modes, precision) -> list[tuple[str, Decimal]]:
	"""[(mode of payment, difference)] of the shift's payment rows whose mode is one of `cash_modes`
	and whose difference at `precision` is not zero, in the shift's order (pure). A difference is
	counted minus expected: negative is short, positive is over."""
	rows = []
	for row in shift_doc.get("payments") or []:
		mode = row.get("mode_of_payment")
		if mode not in cash_modes:
			continue
		difference = quantize(as_decimal(row.get("difference")), precision)
		if difference != 0:
			rows.append((mode, difference))
	return rows


def account_problem(account, company) -> tuple[str, str] | None:
	"""Why `account` cannot take an outlet's cash over/short (the words for the owner), or None: it
	must be an enabled ledger (not a group) of the outlet's company, of root type Expense or Income."""
	if not account:
		return MISSING_ACCOUNT, _("Set the cash over/short account in the outlet's settings")
	row = frappe.db.get_value(
		"Account", account, ["company", "is_group", "root_type", "disabled"], as_dict=True
	)
	if not row:
		return MISSING_ACCOUNT, _("The cash over/short account {0} does not exist").format(account)
	if row.company != company:
		return ACCOUNT_WRONG_COMPANY, _("The cash over/short account {0} is not an account of {1}").format(
			account, company
		)
	if cint(row.is_group):
		return ACCOUNT_NOT_LEAF, _("The cash over/short account {0} is a group, not a ledger").format(account)
	if cint(row.disabled):
		return ACCOUNT_DISABLED, _("The cash over/short account {0} is disabled").format(account)
	if row.root_type not in ACCOUNT_ROOT_TYPES:
		return ACCOUNT_WRONG_TYPE, _(
			"The cash over/short account {0} must be an expense or income account"
		).format(account)
	return None


def validate_profile(doc, method=None):
	"""POS Profile validate hook: a cash over/short account, when set, is one `account_problem`
	accepts (left blank while the switch is on, the outlet's setup checks say so)."""
	account = doc.get(CASH_DIFFERENCE_ACCOUNT_FIELD)
	if account:
		problem = account_problem(account, doc.company)
		if problem:
			frappe.throw(problem[1])


def cash_modes() -> set[str]:
	return set(frappe.get_all("Mode of Payment", filters={"type": "Cash"}, pluck="name"))


def _cash_account(mode, company):
	return frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode, "parenttype": "Mode of Payment", "company": company},
		"default_account",
	)


def _cost_center(profile, company):
	return profile.get("cost_center") or frappe.get_cached_value("Company", company, "cost_center")


def _frozen_till(company):
	# the one rule of the web settle (ERPNext v16 Company field, else v15 Accounts Settings)
	from crenya_pos_api.api.web_admin.credit import frozen_till

	return frozen_till(company)


def existing_entry(shift_name) -> str | None:
	"""The shift's over/short Journal Entry that is not cancelled, or None."""
	return frappe.db.get_value(JOURNAL_ENTRY, {SHIFT_LINK_FIELD: shift_name, "docstatus": ["!=", 2]}, "name")


def _entry_accounts(rows, over_short_account, company, cost_center, precision):
	accounts = []
	for mode, difference in rows:
		cash_account = _cash_account(mode, company)
		if not cash_account:
			raise NotPostable(
				NO_CASH_ACCOUNT, _("Mode of payment {0} has no account for {1}").format(mode, company)
			)
		amount = flt(format_money(abs(difference), precision), precision)
		# short: the drawer holds less than expected, the loss is debited
		cash_side, other_side = ("credit", "debit") if difference < 0 else ("debit", "credit")
		accounts.append(
			{
				"account": cash_account,
				f"{cash_side}_in_account_currency": amount,
				"cost_center": cost_center,
				"user_remark": mode,
			}
		)
		accounts.append(
			{
				"account": over_short_account,
				f"{other_side}_in_account_currency": amount,
				"cost_center": cost_center,
				"user_remark": mode,
			}
		)
	return accounts


def _post(shift, profile, precision):
	"""(state, entry) of the shift as the outlet is set up now; raises NotPostable for a setup
	problem. Posts the entry when one is needed and none exists."""
	if not cint(profile.get(CASH_DIFFERENCE_SWITCH_FIELD)):
		return OFF, None
	rows = cash_rows(shift, cash_modes(), precision)
	if not rows:
		return NOT_NEEDED, None
	found = existing_entry(shift.name)
	if found:
		return POSTED, found
	company = shift.company
	account = profile.get(CASH_DIFFERENCE_ACCOUNT_FIELD)
	problem = account_problem(account, company)
	if problem:
		raise NotPostable(*problem)
	posting_date = getdate(shift.closed_at)
	frozen = _frozen_till(company)
	if frozen and posting_date <= frozen:
		raise NotPostable(
			FROZEN_DATE,
			_("The accounts of {0} are frozen up to {1}: the shift closed on {2}").format(
				company, frozen, posting_date
			),
		)
	cost_center = _cost_center(profile, company)
	if not cost_center:
		raise NotPostable(
			NO_COST_CENTER,
			_("Set a cost center on the outlet or a default cost center of {0}").format(company),
		)
	entry = frappe.new_doc(JOURNAL_ENTRY)
	entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": company,
			"posting_date": posting_date,
			"user_remark": f"crenya pos shift {shift.shift_number} cash over/short",
			SHIFT_LINK_FIELD: shift.name,
		}
	)
	for row in _entry_accounts(rows, account, company, cost_center, precision):
		entry.append("accounts", row)
	# the device or the manager was checked by the caller; the entry is ERPNext's own to validate
	with elevated_as_administrator():
		entry.insert(ignore_permissions=True)
		entry.submit()
	return POSTED, entry.name


def post_for_shift(shift_name: str, lock: bool = True) -> dict:
	"""Post the shift's over/short entry when it is needed and none exists, store the state on the
	shift, and answer `cash_difference_block`. Never raises for a setup problem or for ERPNext
	refusing the entry: the shift says "Not posted" and why.

	`lock` (a Retry from the web): take the shift's database lock first and commit before releasing
	it. A shift being pushed (`create_shift`) calls with lock=False: its own event cannot run twice at
	once (Sync Event), and nobody can retry a shift that is not committed yet."""
	if not lock:
		return _post_and_store(shift_name)
	with _shift_lock(shift_name):
		return _post_and_store(shift_name)


def _post_and_store(shift_name):
	shift = frappe.get_doc(SHIFT_DOCTYPE, shift_name)
	profile = frappe.get_doc("POS Profile", shift.pos_profile)
	# the outlet currency by the device rule (the profile's, else the company's)
	precision = money_precision(DeviceContext(device={}, profile=profile).currency)
	error = None
	frappe.db.savepoint(SAVEPOINT)
	try:
		state, entry = _post(shift, profile, precision)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		# the transaction is gone: the caller's own retry (the till's event, the web request) decides
		raise
	except NotPostable as exc:
		frappe.db.rollback(save_point=SAVEPOINT)
		state, entry, error = NOT_POSTED, None, stored_error(exc.code, str(exc))
	except Exception as exc:
		# ERPNext refused the entry (its words): nothing of it is kept, the shift still stands
		frappe.db.rollback(save_point=SAVEPOINT)
		state, entry, error = NOT_POSTED, None, stored_error(ENTRY_REFUSED, str(exc) or type(exc).__name__)
		# a frappe.throw left its message for the response: the answer says it instead
		frappe.clear_messages()
	values = {"cash_difference_state": state, "cash_difference_entry": entry, "cash_difference_error": error}
	frappe.db.set_value(SHIFT_DOCTYPE, shift.name, values, update_modified=False)
	shift.update(values)
	return cash_difference_block(shift)


def cash_difference_block(shift_doc, can_retry=None) -> dict:
	"""The `cash_difference` block of the Z answer: {state, entry, error, can_retry}. A shift stored
	before 0.17.0 has no state: "Off". Retry is offered for "Not posted" alone."""
	state = shift_doc.get("cash_difference_state") or OFF
	return {
		"state": state,
		"entry": shift_doc.get("cash_difference_entry") or None,
		# a code (ERROR_CODES), never the words: the web translates it
		"error": error_code(shift_doc.get("cash_difference_error")) if state == NOT_POSTED else None,
		"can_retry": (state == NOT_POSTED) if can_retry is None else bool(can_retry),
	}


@contextmanager
def _shift_lock(shift_name):
	"""The shift's named lock, taken after ending the open transaction (so this post reads what an
	earlier holder committed); the work is committed before the lock is released."""
	name = f"{LOCK_PREFIX}{shift_name}"
	frappe.db.commit()  # nosemgrep  # the snapshot must start after the lock is held
	row = frappe.db.sql("select get_lock(%s, %s)", (name, LOCK_WAIT_SECONDS))
	if not (row and row[0][0] == 1):
		frappe.throw(_("The shift's cash over/short is being posted: try again"))
	try:
		yield
	except BaseException:
		frappe.db.rollback()
		_release(name)
		raise
	frappe.db.commit()  # nosemgrep  # before the release: the next holder must see the entry
	_release(name)


def _release(name):
	try:
		frappe.db.sql("select release_lock(%s)", (name,))
	except Exception:
		# the connection is gone: its locks went with it
		pass


def record_for_new_shift(doc, notes) -> None:
	"""Called by `sync.shift.create_shift` after the shift is inserted: post (or not) and keep the
	shift whatever happens, with a note on the Sync Event when nothing was posted for a reason."""
	try:
		block = post_for_shift(doc.name, lock=False)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		raise
	except Exception as exc:
		# post_for_shift never raises for setup problems; anything else must still not lose the shift
		frappe.db.set_value(
			SHIFT_DOCTYPE,
			doc.name,
			{
				"cash_difference_state": NOT_POSTED,
				"cash_difference_error": stored_error(POSTING_FAILED, str(exc)),
			},
			update_modified=False,
		)
		block = {"state": NOT_POSTED, "error": POSTING_FAILED}
	stored = frappe.db.get_value(SHIFT_DOCTYPE, doc.name, "cash_difference_error")
	doc.cash_difference_state = block["state"]
	doc.cash_difference_entry = block.get("entry")
	doc.cash_difference_error = stored
	if block["state"] == NOT_POSTED:
		notes.append(f"Cash over/short not posted: {stored or block['error']}")
	elif block["state"] == POSTED:
		notes.append(f"Cash over/short posted: {block['entry']}")
