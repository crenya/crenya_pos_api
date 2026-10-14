"""Till expenses: what a cashier paid out of the drawer, booked as a paid Purchase Invoice.

The engine is product-neutral and shared by the retail till and the restaurant till (it moved
down here from crenya_restaurant, 0.13.0):

- `booking`: the POS Expense to Purchase Invoice booking, its retry and the setup checks;
- `fields`: the outlet's expense settings on POS Profile;
- `product`: the seam, one `ExpenseProduct` per product (wording, device types, approvers);
- `reports`: the Z report's expense rows.

erptrue_app owns the doctypes (POS Expense, its lines and log, POS Expense Category); this app
reaches them by name only (`booking.expenses_supported`).
"""
