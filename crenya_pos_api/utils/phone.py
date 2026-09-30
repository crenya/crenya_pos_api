"""Dialling code of a company's country, used by the till to complete customer phone numbers."""

DEFAULT_PHONE_COUNTRY_CODE = "+968"

PHONE_COUNTRY_CODES = {
	"Oman": "+968",
	"United Arab Emirates": "+971",
	"Saudi Arabia": "+966",
	"Qatar": "+974",
	"Bahrain": "+973",
	"Kuwait": "+965",
	"India": "+91",
	"Pakistan": "+92",
	"Bangladesh": "+880",
	"Philippines": "+63",
	"Egypt": "+20",
}


def phone_country_code(country):
	"""`+968` style code for an ERPNext Country name; unknown or empty countries fall back to Oman."""
	if not isinstance(country, str):
		return DEFAULT_PHONE_COUNTRY_CODE
	return PHONE_COUNTRY_CODES.get(country.strip(), DEFAULT_PHONE_COUNTRY_CODE)
