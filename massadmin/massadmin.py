# -*- coding: utf-8 -*-
# Updates by David Burke <david@burkesoftware.com>
# Orginal code used to be at http://algoholic.eu/django-mass-change-admin-site-extension/
"""
Copyright (c) 2010, Stanislaw Adaszewskin
Copyright (c) 2012, Libération
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of Stanislaw Adaszewski nor the
      names of any contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL Stanislaw Adaszewski BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from copy import deepcopy
import urllib

from django.contrib import admin
from django.conf.urls.defaults import patterns, url
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db import models
from django.contrib.admin import helpers
from django.utils.translation import ugettext as _
from django.utils.encoding import force_unicode
from django.utils.safestring import mark_safe
from django.template.defaultfilters import pluralize
from django.http import HttpResponseRedirect
from django import  template
from django.shortcuts import render_to_response
from django.forms.formsets import all_valid

from forms import MassOptionsForField

import sys


class MassAdmin(admin.ModelAdmin):
    actions = ['mass_change_selected']
    mass_change_form_template = None
    mass_actions_options_form = MassOptionsForField

    def get_urls(self):
        urls = super(MassAdmin, self).get_urls()
        info = self.model._meta.app_label, self.model._meta.module_name
        custom_urls = patterns('',
            url(r'^masschange/(?P<object_ids>[0-9,]+)/$',
                self.admin_site.admin_view(self.mass_change_view),
                name='%s_%s_massadmin' % info)
        )
        return custom_urls + urls

    def mass_change_selected(self, request, queryset):
        selected = request.POST.getlist(admin.ACTION_CHECKBOX_NAME)
        info = self.model._meta.app_label, self.model._meta.module_name
        selected = ','.join(selected)
        massadmin_url = reverse('admin:%s_%s_massadmin' % info, args=(selected,), current_app=self.admin_site.name)

        next = urllib.quote(request.get_full_path())

        massadmin_url = '%s?next=%s' % (massadmin_url, next)

        return HttpResponseRedirect(massadmin_url)
    mass_change_selected.short_description = _('Mass change selected')

    def response_mass_change(self, request, obj):
        opts = obj._meta
        msg = _('Selected %(name)s were changed successfully.') % {'name': force_unicode(opts.verbose_name_plural), 'obj': force_unicode(obj)}
        self.message_user(request, msg)

        next = request.GET.get('next', '../../')

        return HttpResponseRedirect(next)

    def render_mass_change_form(self, request, context, obj=None):
        opts = self.model._meta
        app_label = opts.app_label
        ordered_objects = opts.get_ordered_objects()
        context.update({
            'admin_site': self.admin_site,
            'add': True,
            'has_add_permission': self.has_add_permission(request),
            'has_change_permission': self.has_change_permission(request, obj),
            'has_delete_permission': self.has_delete_permission(request, obj),
            'has_file_field': True,
            'ordered_objects': ordered_objects,
            'opts': opts,
            'save_on_top': self.save_on_top,
            'root_path': self.admin_site.root_path,
            'onclick_attrib': (opts.get_ordered_objects() and change and 'onclick="submitOrderForm();"' or ''),
        })
        context_instance = template.RequestContext(request, current_app=self.admin_site.name)
        return render_to_response(self.mass_change_form_template or [
            "admin/%s/%s/mass_change_form.html" % (app_label, opts.object_name.lower()),
            "admin/%s/mass_change_form.html" % app_label,
            "admin/mass_change_form.html"
        ], context, context_instance=context_instance)

    def get_mass_form(self, request, obj=None):
        return self.get_form(request, obj)

    def mass_change_view(self, request, object_ids=None, extra_context=None):
        """The 'mass change' admin view for this model."""
        model = self.model
        opts = model._meta
        general_error = None

        object_ids = object_ids.split(',')

        if not self.has_change_permission(request, None):  # FIXME: use a specific permission for mass_change
            raise PermissionDenied

        ModelForm = self.get_mass_form(request)
        formsets = []
        if request.method == 'POST':

            # Store which fields are handled on this mass change.
            # Also, store optionnal mass actions (replace, preprend,
            # append, etc.) on those fields.
            # FIXME: doing this on ModelForm *class*'s base_fields saves us
            # from doing it for each form, but on the other hand,
            # form.__init__ could change some fields or widget at instanciation
            # time... We should move back this work inside looping of each form,
            # but trying to do it only once...
            exclude_fields = []
            special_handled_fields = {}  # format -- {'<field name>': '<action>'}
            for fieldname, field in ModelForm.base_fields.items():
                mass_options_form = self.mass_actions_options_form(data=request.POST, field=field, field_name=fieldname)
                if mass_options_form.is_valid():
                    mass_field_name = mass_options_form.get_mass_field_name()
                    handle_mass_change = mass_options_form.cleaned_data.get(mass_field_name, False)
                    if handle_mass_change:
                        action = mass_options_form.cleaned_data.get(mass_field_name + '_action', None)
                        if action:
                            special_handled_fields[fieldname] = action
                    else:
                        exclude_fields.append(fieldname)
                else:
                    raise Exception('Mass options for field %s are not valid: %s ' % (fieldname, mass_options_form.errors))

            # commit only when all forms are valid
            with transaction.commit_manually():
                try:
                    objects_count = 0
                    changed_count = 0

                    objects = self.queryset(request).filter(pk__in=object_ids)
                    for obj in objects:
                        objects_count += 1
                        form = ModelForm(request.POST, request.FILES, instance=obj)

                        for fieldname in exclude_fields:
                            del form.fields[fieldname]

                        self._handle_fields_action(form, obj, special_handled_fields)

                        if form.is_valid():
                            form_validated = True
                            new_object = self.save_form(request, form, change=True)
                        else:
                            form_validated = False
                            new_object = obj

                        prefixes = {}
                        for FormSet in self.get_formsets(request, new_object):
                            prefix = FormSet.get_default_prefix()
                            prefixes[prefix] = prefixes.get(prefix, 0) + 1
                            if prefixes[prefix] != 1:
                                prefix = "%s-%s" % (prefix, prefixes[prefix])

                            # Check if inline formset has been selected for
                            # mass change. If it is the case, store it
                            # for later use
                            mass_options_form = self.mass_actions_options_form(data=request.POST, field_name=prefix)
                            if mass_options_form.is_valid():
                                mass_field_name = mass_options_form.get_mass_field_name()
                                handle_mass_change = mass_options_form.cleaned_data.get(mass_field_name, False)
                                if handle_mass_change:
                                    formset = FormSet(request.POST, request.FILES, instance=new_object, prefix=prefix)
                                    formsets.append(formset)
                            else:
                                raise Exception('Mass options for inline %s are not valid: %s' % (prefix, mass_options_form.errors))

                        if all_valid(formsets) and form_validated:
                            self.save_model(request, new_object, form, change=True)
                            form.save_m2m()
                            for formset in formsets:
                                self.save_formset(request, form, formset, change=True)

                            change_message = self.construct_change_message(request, form, formsets)
                            self.log_change(request, new_object, change_message)
                            changed_count += 1

                    if changed_count != objects_count:
                        raise Exception('Some of the selected objects could\'t be changed.')
                    transaction.commit()
                    return self.response_mass_change(request, new_object)

                finally:
                    general_error = unicode(sys.exc_info()[1])
                    transaction.rollback()

        form = ModelForm()
        prefixes = {}
        for FormSet in self.get_formsets(request):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset = FormSet(prefix=prefix)
            formsets.append(formset)

        adminForm = helpers.AdminForm(form, self.get_fieldsets(request), self.prepopulated_fields, self.get_readonly_fields(request))
        media = self.media + adminForm.media

        # We don't want the user trying to mass change unique fields!
        unique_fields = []
        for field_name in model._meta.get_all_field_names():
            try:
                field = model._meta.get_field(field_name)
                if field.unique:
                    unique_fields.append(field_name)
            except:
                pass

        inline_admin_formsets = []
        for inline, formset in zip(self.inline_instances, formsets):
            fieldsets = list(inline.get_fieldsets(request))
            inline_admin_formset = helpers.InlineAdminFormSet(inline, formset, fieldsets)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media

        n_objects = len(object_ids)
        context = {
            'title': _('Mass change of %(n_objects)s %(verbose_name)s%(plural)s') % {'n_objects': n_objects, 'verbose_name': force_unicode(opts.verbose_name), 'plural': pluralize(n_objects)},
            'adminform': adminForm,
            'unique_fields': unique_fields,
            'is_popup': request.REQUEST.has_key('_popup'),
            'media': mark_safe(media),
            'inline_admin_formsets': inline_admin_formsets,
            'errors': helpers.AdminErrorList(form, formsets),
            'general_error': general_error,
            'root_path': self.admin_site.root_path,
            'app_label': opts.app_label,
            'object_ids': ",".join(object_ids),
        }
        context.update(extra_context or {})
        return self.render_mass_change_form(request, context)

    def _handle_fields_action(self, form, obj, special_handled_fields):
        """
        If there are some fields that need special
        action (prepend, append, etc.), make a deepcopy
        of POST data and alter it accordingly *before*
        calling ModelForm.is_valid() (which is
        responsible for *using* and cleaning POST data).
        """
        if special_handled_fields:
            form.data = deepcopy(form.data)
            for fieldname, action in special_handled_fields.items():
                if isinstance(obj._meta.get_field_by_name(fieldname)[0], models.ManyToManyField):
                    ACTIONS = self.mass_actions_options_form.MULTI_ACTIONS
                    if action == ACTIONS.ADD:
                        for val in form.initial[fieldname]:
                            val = unicode(val)  # Form values are always string, not int
                            if not val in form.data.getlist(fieldname):
                                form.data.appendlist(fieldname, val)
                    elif action == ACTIONS.DEFINE:
                        if getattr(obj, fieldname).all():
                            del form.fields[fieldname]
                    elif action == ACTIONS.REPLACE:
                        pass  # replace is the default action
                else:
                    ACTIONS = self.mass_actions_options_form.CHARFIELD_ACTIONS
                    if action == ACTIONS.PREPEND:
                        form.data[fieldname] = form.data[fieldname] + getattr(obj, fieldname, '')
                    elif action == ACTIONS.APPEND:
                        form.data[fieldname] = getattr(obj, fieldname, '') + form.data[fieldname]
                    elif action == ACTIONS.DEFINE:
                        if getattr(obj, fieldname, ''):
                            # if obj has already a value for this
                            # field, don't handle mass change for it
                            del form.fields[fieldname]
                    elif action == ACTIONS.REPLACE:
                        pass  # replace is the default action
