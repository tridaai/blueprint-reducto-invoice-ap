"""Trida AI Blueprint — Reducto Invoice AP, deployed in-VPC.

Accounts payable on Reducto's document platform, deployed inside
the customer's own boundary: documents are classified, split, and
extracted behind a DocumentPort, matched three ways in code, and
stopped at a named approver. Nothing in this package executes a
payment.
"""

__version__ = "0.1.0"
