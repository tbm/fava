"""Provide data suitable for Fava's charts. """
from datetime import date, datetime

from beancount.core import flags, account_types, convert, realization
from beancount.core.amount import Amount
from beancount.core.data import Transaction
from beancount.core.number import Decimal
from beancount.core.position import Position
from beancount.core.inventory import Inventory
from beancount.core.data import iter_entry_dates
from beancount.parser import options
from flask.json import JSONEncoder

from fava.util import pairwise
from fava.core.helpers import FavaModule
from fava.core.holdings import inventory_at_dates


class FavaJSONEncoder(JSONEncoder):
    def default(self, o):  # pylint: disable=E0202
        if isinstance(o, datetime):
            return o.strftime('%Y-%m-%dT%H:%M:%SZ')
        elif isinstance(o, date):
            return o.strftime('%Y-%m-%d')
        elif isinstance(o, Decimal):
            return float(o)
        elif isinstance(o, Amount):
            return str(o)
        elif isinstance(o, Position):
            return str(o)
        elif isinstance(o, (set, frozenset)):
            return list(o)
        try:
            return JSONEncoder.default(self, o)
        except TypeError:
            # workaround for #472
            try:
                return str(o)
            except TypeError:
                return ''


def _serialize_inventory(inventory, at_cost=False):
    """Renders an Inventory to a currency -> amount dict."""
    if at_cost:
        inventory = inventory.reduce(convert.get_cost)
    else:
        inventory = inventory.reduce(convert.get_units)
    return {p.units.currency: p.units.number for p in inventory}


def _real_account(account_name, entries, begin_date, end_date):
    if begin_date:
        entries = list(iter_entry_dates(entries, begin_date, end_date))

    return realization.get_or_create(
        realization.realize(entries), account_name)


def _serialize_real_account(real_account):
    return {
        'account': real_account.account,
        'balance_children': _serialize_inventory(
            realization.compute_balance(real_account), at_cost=True),
        'balance': _serialize_inventory(real_account.balance, at_cost=True),
        'children':
        [_serialize_real_account(a) for n, a in sorted(real_account.items())],
    }


class ChartModule(FavaModule):
    __slots__ = ['ledger']

    def _total_balance(self, names, begin_date, end_date):
        totals = [
            realization.compute_balance(
                _real_account(account_name, self.ledger.entries, begin_date,
                              end_date)) for account_name in names
        ]
        return _serialize_inventory(sum(totals, Inventory()), at_cost=True)

    def events(self, event_type):
        return [{
            'type': entry.type,
            'date': entry.date,
            'description': entry.description
        } for entry in self.ledger.events(event_type)]

    def hierarchy(self, account_name, begin_date=None, end_date=None):
        real_account = _real_account(account_name, self.ledger.entries,
                                     begin_date, end_date)
        return _serialize_real_account(real_account)

    def interval_totals(self, interval, account_name):
        """Renders totals for account (or accounts) in the intervals."""
        if isinstance(account_name, str):
            names = [account_name]
        else:
            names = account_name

        interval_tuples = pairwise(self.ledger.interval_ends(interval))
        return [{
            'begin_date': begin_date,
            'totals': self._total_balance(names, begin_date, end_date),
            'budgets': self.ledger.budgets.calculate(names[0], begin_date,
                                                     end_date),
        } for begin_date, end_date in interval_tuples]

    def linechart(self, account_name):
        real_account = realization.get_or_create(self.ledger.root_account,
                                                 account_name)
        postings = realization.get_postings(real_account)
        journal = realization.iterate_with_balance(postings)

        # When the balance for a commodity just went to zero, it will be
        # missing from the 'balance' field but appear in the 'change' field.
        # Use 0 for those commodities.
        return [{
            'date': entry.date,
            'balance': dict({curr: 0
                             for curr in list(change.currencies())},
                            **_serialize_inventory(balance)),
        } for entry, _, change, balance in journal if len(change)]

    def net_worth_at_dates(self, interval):
        transactions = [
            entry for entry in self.ledger.entries
            if (isinstance(entry, Transaction) and entry.flag !=
                flags.FLAG_UNREALIZED)
        ]

        types = options.get_account_types(self.ledger.options)

        def _posting_predicate(posting):
            account_type = account_types.get_account_type(posting.account)
            if account_type in (types.assets, types.liabilities):
                return True

        dates = self.ledger.interval_ends(interval)
        inventories = inventory_at_dates(transactions, dates,
                                         _posting_predicate)

        return [{
            'date': date,
            'balance': {
                currency: inv.reduce(convert.convert_position, currency,
                                     self.ledger.price_map,
                                     date).get_currency_units(currency).number
                for currency in self.ledger.options['operating_currency']
            }
        } for date, inv in zip(dates, inventories)]
