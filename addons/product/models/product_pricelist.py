# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import get_lang


class Pricelist(models.Model):
    _name = "product.pricelist"
    _description = "Pricelist"
    _order = "sequence asc, id desc"

    def _default_currency_id(self):
        return self.env.company.currency_id.id

    name = fields.Char(string="Pricelist Name", required=True, translate=True)

    active = fields.Boolean(
        string="Active",
        default=True,
        help="If unchecked, it will allow you to hide the pricelist without removing it.")
    sequence = fields.Integer(default=16)

    currency_id = fields.Many2one(
        comodel_name='res.currency',
        default=_default_currency_id,
        required=True)

    company_id = fields.Many2one(
        comodel_name='res.company')
    country_group_ids = fields.Many2many(
        comodel_name='res.country.group',
        relation='res_country_group_pricelist_rel',
        column1='pricelist_id',
        column2='res_country_group_id',
        string="Country Groups")

    discount_policy = fields.Selection(
        selection=[
            ('with_discount', "Discount included in the price"),
            ('without_discount', "Show public price & discount to the customer"),
        ],
        default='with_discount',
        required=True)

    item_ids = fields.One2many(
        comodel_name='product.pricelist.item',
        inverse_name='pricelist_id',
        string="Pricelist Rules",
        copy=True)

    def name_get(self):
        return [(pricelist.id, '%s (%s)' % (pricelist.name, pricelist.currency_id.name)) for pricelist in self]

    @api.model
    def _name_search(self, name, args=None, operator='ilike', limit=100, name_get_uid=None):
        if name and operator == '=' and not args:
            # search on the name of the pricelist and its currency, opposite of name_get(),
            # Used by the magic context filter in the product search view.
            query_args = {'name': name, 'limit': limit, 'lang': get_lang(self.env).code}
            query = """SELECT p.id
                       FROM ((
                                SELECT pr.id, pr.name
                                FROM product_pricelist pr JOIN
                                     res_currency cur ON
                                         (pr.currency_id = cur.id)
                                WHERE pr.name || ' (' || cur.name || ')' = %(name)s
                            )
                            UNION (
                                SELECT tr.res_id as id, tr.value as name
                                FROM ir_translation tr JOIN
                                     product_pricelist pr ON (
                                        pr.id = tr.res_id AND
                                        tr.type = 'model' AND
                                        tr.name = 'product.pricelist,name' AND
                                        tr.lang = %(lang)s
                                     ) JOIN
                                     res_currency cur ON
                                         (pr.currency_id = cur.id)
                                WHERE tr.value || ' (' || cur.name || ')' = %(name)s
                            )
                        ) p
                       ORDER BY p.name"""
            if limit:
                query += " LIMIT %(limit)s"
            self._cr.execute(query, query_args)
            ids = [r[0] for r in self._cr.fetchall()]
            # regular search() to apply ACLs - may limit results below limit in some cases
            pricelist_ids = self._search([('id', 'in', ids)], limit=limit, access_rights_uid=name_get_uid)
            if pricelist_ids:
                return pricelist_ids
        return super()._name_search(name, args, operator=operator, limit=limit, name_get_uid=name_get_uid)

    def _get_products_price(self, products, quantity, uom=None, date=False):
        """Compute the pricelist prices for the specified products, qty & uom.

        Note: self.ensure_one()

        :returns: dict{product_id: product price}, considering the current pricelist
        :rtype: dict
        """
        self.ensure_one()
        return {
            product_id: res_tuple[0]
            for product_id, res_tuple in self._compute_price_rule(
                products,
                quantity,
                uom=uom,
                date=date,
            ).items()
        }

    def _get_product_price(self, product, quantity, uom=None, date=False):
        """Compute the pricelist price for the specified product, qty & uom.

        Note: self.ensure_one()

        :returns: unit price of the product, considering pricelist rules
        :rtype: float
        """
        self.ensure_one()
        return self._compute_price_rule(product, quantity, uom=uom, date=date)[product.id][0]

    def _get_product_price_rule(self, product, quantity, uom=None, date=False):
        """Compute the pricelist price & rule for the specified product, qty & uom.

        Note: self.ensure_one()

        :returns: (product unit price, applied pricelist rule id)
        :rtype: tuple(float, int)
        """
        self.ensure_one()
        return self._compute_price_rule(product, quantity, uom=uom, date=date)[product.id]

    def _get_product_rule(self, product, quantity, uom=None, date=False):
        """Compute the pricelist price & rule for the specified product, qty & uom.

        Note: self.ensure_one()

        :returns: applied pricelist rule id
        :rtype: int or False
        """
        self.ensure_one()
        return self._compute_price_rule(product, quantity, uom=uom, date=date)[product.id][1]

    def _compute_price_rule(self, products, qty, uom=None, date=False):
        """ Low-level method - Mono pricelist, multi products
        Returns: dict{product_id: (price, suitable_rule) for the given pricelist}

        :param products: recordset of products (product.product/product.template)
        :param float qty: quantity of products requested (in given uom)
        :param uom: unit of measure (uom.uom record)
            If not specified, prices returned are expressed in product uoms
        :param date: date to use for price computation and currency conversions
        :type date: date or datetime

        :returns: product_id: (price, pricelist_rule)
        :rtype: dict
        """
        self.ensure_one()

        if not products:
            return {}

        if not date:
            # Used to fetch pricelist rules and currency rates
            date = fields.Datetime.now()

        categ_ids = {}
        for p in products:
            categ = p.categ_id
            while categ:
                categ_ids[categ.id] = True
                categ = categ.parent_id
        category_ids = list(categ_ids)

        is_product_template = products[0]._name == "product.template"
        if is_product_template:
            product_tmpl_ids = products.ids
            # all variants of all products
            product_ids = products.product_variant_ids.ids
        else:
            product_ids = products.ids
            product_tmpl_ids = products.product_tmpl_id.ids

        # Fetch all rules potentially matching specified products/templates/categories and date
        rules = self._get_applicable_rules(date, product_tmpl_ids, product_ids, category_ids)

        results = {}
        for product in products:
            suitable_rule = self.env['product.pricelist.item']

            product_uom = product.uom_id
            target_uom = uom or product_uom  # If no uom is specified, fall back on the product uom

            # Compute quantity in product uom because pricelist rules are specified
            # w.r.t product default UoM (min_quantity, price_surchage, ...)
            if target_uom != product_uom:
                qty_in_product_uom = target_uom._compute_quantity(qty, product_uom, raise_if_failure=False)
            else:
                qty_in_product_uom = qty

            for rule in rules:
                if rule._is_applicable_for(product, qty_in_product_uom):
                    suitable_rule = rule
                    break

            # TODO VFE provide a way for lazy computation of price ?
            if suitable_rule:
                price = suitable_rule._compute_price(product, qty, target_uom, date)
            else:
                # fall back on Sales Price if no rule is found
                price = product.price_compute('list_price', uom=target_uom, date=date)[product.id]

                if product.currency_id != self.currency_id:
                    price = product.currency_id._convert(price, self.currency_id, self.env.company, date, round=False)

            results[product.id] = (price, suitable_rule.id)

        return results

    # Split methods to ease (community) overrides
    def _get_applicable_rules(self, *args, **kwargs):
        self.ensure_one()
        # Do not filter out archived pricelist items, since it means current pricelist is also archived
        # We do not want the computation of prices for archived pricelist to always fallback on the Sales price
        # because no rule was found (thanks to the automatic orm filtering on active field)
        return self.env['product.pricelist.item'].with_context(active_test=False).search(
            self._get_applicable_rules_domain(*args, **kwargs)
        )

    def _get_applicable_rules_domain(self, date, product_tmpl_ids, product_ids, category_ids):
        return [
            ('pricelist_id', '=', self.id),
            '|', ('product_tmpl_id', '=', False), ('product_tmpl_id', 'in', product_tmpl_ids),
            '|', ('product_id', '=', False), ('product_id', 'in', product_ids),
            '|', ('categ_id', '=', False), ('categ_id', 'in', category_ids),
            '|', ('date_start', '=', False), ('date_start', '<=', date),
            '|', ('date_end', '=', False), ('date_end', '>=', date),
        ]

    # Multi pricelists price|rule computation
    def _price_get(self, product, qty):
        """ Multi pricelist, mono product - returns price per pricelist """
        return {
            key: price[0]
            for key, price in self._compute_price_rule_multi(product, qty)[product.id].items()}

    def _compute_price_rule_multi(self, products, qty, uom=None, date=False):
        """ Low-level method - Multi pricelist, multi products
        Returns: dict{product_id: dict{pricelist_id: (price, suitable_rule)} }"""
        if not self.ids:
            pricelists = self.search([])
        else:
            pricelists = self
        results = {}
        for pricelist in pricelists:
            subres = pricelist._compute_price_rule(products, qty, uom=uom, date=date)
            for product_id, price in subres.items():
                results.setdefault(product_id, {})
                results[product_id][pricelist.id] = price
        return results

    # res.partner.property_product_pricelist field computation
    @api.model
    def _get_partner_pricelist_multi(self, partner_ids, company_id=None):
        """ Retrieve the applicable pricelist for given partners in a given company.

        It will return the first found pricelist in this order:
        First, the pricelist of the specific property (res_id set), this one
                is created when saving a pricelist on the partner form view.
        Else, it will return the pricelist of the partner country group
        Else, it will return the generic property (res_id not set), this one
                is created on the company creation.
        Else, it will return the first available pricelist

        :param int company_id: if passed, used for looking up properties,
            instead of current user's company
        :return: a dict {partner_id: pricelist}
        """
        # `partner_ids` might be ID from inactive users. We should use active_test
        # as we will do a search() later (real case for website public user).
        Partner = self.env['res.partner'].with_context(active_test=False)
        company_id = company_id or self.env.company.id

        Property = self.env['ir.property'].with_company(company_id)
        Pricelist = self.env['product.pricelist']
        pl_domain = self._get_partner_pricelist_multi_search_domain_hook(company_id)

        # if no specific property, try to find a fitting pricelist
        result = Property._get_multi('property_product_pricelist', Partner._name, partner_ids)

        remaining_partner_ids = [pid for pid, val in result.items() if not val or
                                 not val._get_partner_pricelist_multi_filter_hook()]
        if remaining_partner_ids:
            # get fallback pricelist when no pricelist for a given country
            pl_fallback = (
                Pricelist.search(pl_domain + [('country_group_ids', '=', False)], limit=1) or
                Property._get('property_product_pricelist', 'res.partner') or
                Pricelist.search(pl_domain, limit=1)
            )
            # group partners by country, and find a pricelist for each country
            domain = [('id', 'in', remaining_partner_ids)]
            groups = Partner.read_group(domain, ['country_id'], ['country_id'])
            for group in groups:
                country_id = group['country_id'] and group['country_id'][0]
                pl = Pricelist.search(pl_domain + [('country_group_ids.country_ids', '=', country_id)], limit=1)
                pl = pl or pl_fallback
                for pid in Partner.search(group['__domain']).ids:
                    result[pid] = pl

        return result

    def _get_partner_pricelist_multi_search_domain_hook(self, company_id):
        return [
            ('active', '=', True),
            ('company_id', 'in', [company_id, False]),
        ]

    def _get_partner_pricelist_multi_filter_hook(self):
        return self.filtered('active')

    @api.model
    def get_import_templates(self):
        return [{
            'label': _('Import Template for Pricelists'),
            'template': '/product/static/xls/product_pricelist.xls'
        }]

    @api.ondelete(at_uninstall=False)
    def _unlink_except_used_as_rule_base(self):
        linked_items = self.env['product.pricelist.item'].sudo().with_context(active_test=False).search([
            ('base', '=', 'pricelist'),
            ('base_pricelist_id', 'in', self.ids),
            ('pricelist_id', 'not in', self.ids),
        ])
        if linked_items:
            raise UserError(_(
                'You cannot delete those pricelist(s):\n(%s)\n, they are used in other pricelist(s):\n%s',
                '\n'.join(linked_items.base_pricelist_id.mapped('display_name')),
                '\n'.join(linked_items.pricelist_id.mapped('display_name'))
            ))
