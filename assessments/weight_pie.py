"""The rubric's weight distribution as pie slices: group, individual and what is still free.

Deliberately not one slice per category: a rubric can have dozens of categories and
the pie would turn to confetti. The category list already shows each weight.
"""
from decimal import Decimal

from .models import TOTAL_WEIGHT, RubricCategory

GROUP_COLOUR = '#2563eb'
INDIVIDUAL_COLOUR = '#16a34a'
FREE_COLOUR = '#d4d4d8'


def _number(value):
    text = format(value.quantize(Decimal('0.01')), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def weight_pie(categories):
    """``{'slices': [...], 'gradient': css, 'free': Decimal, 'over': bool}``.

    Slices are Group, Individual (each only when it has weight) and a grey "not
    allocated yet" slice while the total is under 100. Over 100 (an old draft) the
    slices are shrunk to fit so the pie is still a whole circle.
    """
    totals = {RubricCategory.Scope.GROUP: Decimal('0'), RubricCategory.Scope.INDIVIDUAL: Decimal('0')}
    for category in categories:
        totals[category.scope] += category.weight
    total = sum(totals.values(), Decimal('0'))
    free = max(TOTAL_WEIGHT - total, Decimal('0'))
    scale = max(total, TOTAL_WEIGHT)
    pieces = [('Group', totals[RubricCategory.Scope.GROUP], GROUP_COLOUR),
              ('Individual', totals[RubricCategory.Scope.INDIVIDUAL], INDIVIDUAL_COLOUR),
              ('Not allocated yet', free, FREE_COLOUR)]
    slices = [{'name': name, 'weight': weight, 'label': _number(weight), 'colour': colour,
               'is_free': colour == FREE_COLOUR}
              for name, weight, colour in pieces if weight]
    stops, start = [], Decimal('0')
    for piece in slices:
        end = start + piece['weight'] / scale * 100
        stops.append(f"{piece['colour']} {start:.3f}% {end:.3f}%")
        start = end
    return {'slices': slices, 'gradient': ', '.join(stops) or f'{FREE_COLOUR} 0% 100%',
            'free': free, 'free_label': _number(free), 'over': total > TOTAL_WEIGHT}
