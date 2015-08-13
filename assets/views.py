from django.http import HttpResponse, HttpResponseRedirect
from django.template import RequestContext
from django.shortcuts import render_to_response
from django.contrib.auth.decorators import login_required
#from django.db.models.related import RelatedObject
from django.db.models.fields.related import ForeignObjectRel
from django import forms

import csv
import re
import cStringIO
import codecs
from django_auth_ldap.backend import LDAPBackend
from datetime import datetime
from StringIO import StringIO

from restless.models import User
import models

class ImportForm(forms.Form):
    assets_to_import = forms.FileField()

def validate_import(fileobj):
    """
    Performs validation on the CSV file at fileobj.

    Returns a tuple (num_assets, errors, warnings, notes). 
    """

    try:
        c = csv.DictReader(fileobj)
        c.fieldnames
    except Exception:
        errors = ["The file you uploaded could not be interpreted. Check that you uploaded the correct file (in a .csv format) and try again.", ]
        return (0, errors, [], [])

    critical_fields = ('asset_tag', 'manufacturer', 'model', 'serial', 'date_purchased', 'location_name', 'location_block', 'location_site')
    all_fields = critical_fields + ('finance_asset_tag', 'model_lifecycle', 'model_type', 'status', 'purchased_value', 'assigned_user', 'notes')

    # Initialise an LDAP connection
    ldapbackend = LDAPBackend()

    # List to hold errors found during the validation process
    errors = []
    warnings = []
    notes = []

    # List of asset tags, to confirm uniqueness
    asset_tag_list = []

    # Inspect the first row to see what columns we've got
    for field in critical_fields:
        if field not in c.fieldnames:
            errors.append("The mandatory column %s is missing from the spreadsheet." % (field))
    
    for field in c.fieldnames:
        if field not in all_fields:
            warnings.append("Your spreadsheet contains an unknown column '%s'. This column will be ignored during the import process." % (field))

    if not 'status' in c.fieldnames:
        warnings.append("Your spreadsheet does not contain a column called 'status' - the status field of every new asset will be set to 'In storage'.")

    # Inspect each row and do field-specific validation
    for row in c:
        # Check asset tag syntax
        asset_tag_re = re.compile("^IT\d{5}$")
        try:
            if not row['asset_tag']:
                errors.append("Line %d: A value for the asset_tag column is missing. Enter a value to continue." % (c.line_num))
            elif not asset_tag_re.match(row['asset_tag'].upper()):
                errors.append("Line %d: The value '%s' in the asset_tag column is invalid. Asset tags should be in the form ITXXXXX." % (c.line_num, row['asset_tag']))
            if row['asset_tag'].upper() in asset_tag_list:
                errors.append("Line %d: The asset tag '%s' exists in several locations in the spreadsheet. Asset tags are unique - remove the duplicate values to continue." % (c.line_num, row['asset_tag']))

            asset_tag_list.append(row['asset_tag'].upper())

            if models.Asset.objects.get(asset_tag__iexact = row['asset_tag']):
                errors.append("Line %d: The asset tag '%s' already exists in the database. Asset tags must be unique." % (c.line_num, row['asset_tag']))
        except KeyError:
            # Missing fields will have been caught above
            pass
        except models.Asset.DoesNotExist:
            # This is ok, it means there's no duplicate asset in the database
            pass

        # Check finance_asset_tag
        try:
            if row['finance_asset_tag']:
                finance_asset_tag_re = re.compile("^\d+$")
                if not finance_asset_tag_re.match(row['finance_asset_tag']):
                    warnings.append("Line %d: The finance asset tag '%s' contains numbers and other characters - these tags usually only contain numbers. Check the tag is correct before proceeding." % (c.line_num, row['finance_asset_tag']))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check manufacturer
        try:
            if not row['manufacturer']:
                errors.append("Line %d: The mandatory field 'manufacturer' is blank." % (c.line_num))
            if not models.Supplier.objects.filter(name__iexact = row['manufacturer']) and row['manufacturer']:
                notes.append("Manufacturer '%s' on line %d is unknown - a new manufacturer record will be created." % (row['manufacturer'], c.line_num))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check model
        try:
            if not row['model']:
                errors.append("Line %d: The mandatory field 'model' is blank." % (c.line_num))
            if not models.Model.objects.filter(manufacturer__name__iexact = row['manufacturer']).filter(model__iexact = row['model']) and row['manufacturer'] and row['model']:
                notes.append("Model '%s %s' on line %d is unknown - a new model record will be created." % (row['manufacturer'], row['model'], c.line_num))
            if not models.Model.objects.filter(manufacturer__name__iexact = row['manufacturer']).filter(model__iexact = row['model']) and row['manufacturer'] and row['model'] and ('model_lifecycle' not in row.keys() or not row['model_lifecycle']):
                errors.append("Line %d: A new model is to be created, and model_lifecycle has not been specified. Enter a value to continue." % (c.line_num))
            if not models.Model.objects.filter(manufacturer__name__iexact = row['manufacturer']).filter(model__iexact = row['model']) and row['manufacturer'] and row['model'] and ('model_type' not in row.keys() or not row['model_type']):
                errors.append("Line %d: A new model is to be created, and model_type has not been specified. Enter a value to continue." % (c.line_num))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check model_lifecycle
        try:
            i = int(row['model_lifecycle'])
            if i < 0:
                raise ValueError
        except KeyError:
            # Missing fields will have been caught above
            pass
        except ValueError:
            # An error is generated above (under model) if model_type is blank
            if row['model_lifecycle']:
                errors.append("Line %d: The value '%s' in the model_lifecycle column is invalid. The model_lifecycle field should consist of a single positive number." % (c.line_num, row['model_lifecycle']))

        # Check model_type
        try:
            # An error is generated above (under model) if model_type is blank
            if row['model_type'] and (row['model_type'], row['model_type']) not in models.Model.type_choices:
                errors.append("Line %d: The value '%s' in the model_type column is not a valid category. Check the <a href='/assets/categories'>list of categories</a> and correct the value. Note this field is case-sensitive." % (c.line_num, row['model_type']))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check status
        try:
            s = row['status'].capitalize()
            if s != 'In storage' and s != 'Deployed' and s != 'Disposed':
                errors.append("Line %d: The value '%s' in the status column is invalid. The asset status must be one of 'In storage', 'Deployed' or 'Disposed'." % (c.line_num, row['status']))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check serial
        try:
            if not row['serial']:
                errors.append("Line %d: The mandatory field 'serial' is blank. If the device does not have a serial number, enter 'Unknown'." % (c.line_num))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check date_purchased 
        try:
            if not row['date_purchased']:
                errors.append("Line %d: The mandatory field 'date_purchased' is blank." % (c.line_num))
            datetime.strptime(row['date_purchased'], '%d/%m/%Y')
        except KeyError:
            # Missing fields will have been caught above
            pass
        except ValueError:
            errors.append("Line %d: The value '%s' in the date_purchased column is invalid. Dates must be in the form dd/mm/yyyy." % (c.line_num, row['date_purchased']))

        # Check purchased_value
        try:
            purchased_value_re = re.compile("^([0-9]*|\d*\.\d{1}?\d*)$")
            if not purchased_value_re.match(row['purchased_value'].strip()):
                errors.append("Line %d: The value '%s' in the purchased_value column is invalid. Values must be a simple positive decimal number (no $ sign or commas)." % (c.line_num, row['purchased_value'].strip()))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check location fields
        try:
            if not models.Location.objects.filter(name__iexact = row['location_name']).filter(block__iexact = row['location_block']).filter(site__iexact = row['location_site']):
                errors.append("Line %d: There is no defined location matching %s, %s, %s. Locations must be pre-defined in the Locations table before importing data." % (c.line_num, row['location_name'], row['location_block'], row['location_site']))
        except KeyError:
            # Missing fields will have been caught above
            pass

        # Check assigned_user - make sure everything's a valid LDAP user
        try:
            # This might be a bad way of doing this - the user table will be
            # populated with lots of people from LDAP
            ldapbackend.populate_user(row['assigned_user'])
            User.objects.get(username = row['assigned_user'])
        except KeyError:
            # Missing fields will have been caught above
            pass
        except User.DoesNotExist:
            errors.append("Line %d: The username '%s' in column assigned_user does not exist. Ensure it refers to a valid AD user." % (c.line_num, row['assigned_user']))

        # No validation required on notes

    # Reset fileobj now we're finished with it
    fileobj.seek(0)

    return (len(asset_tag_list), errors, warnings, notes)

@login_required(redirect_field_name="")
def do_import(request):
    """
    Receives a POST request from the user, indicating that they would like to
    proceed with the import.
    """

    if request.method != "POST":
        return HttpResponseRedirect("/assets/import")

    # Build a file object from the CSV data in POST and validate the input
    fileobj = StringIO(request.POST['csv'])
    (num_assets, errors, warnings, notes) = validate_import(fileobj)

    if errors:
        return render_to_response("assets/import_criticalerrors.html", {'errors':errors, 'warnings':warnings, 'notes':notes, 'title':'Bulk Import'})

    assets_created = []

    c = csv.DictReader(fileobj)
    for row in c:
        # Get the manufacturer and model first, and create if required
        try:
            ma = models.Supplier.objects.filter(name__iexact = row['manufacturer'])[0]
        except IndexError:
            ma = models.Supplier(name=row['manufacturer'])
            ma.save()

        try:
            mo = models.Model.objects.filter(manufacturer = ma, model__iexact = row['model'])[0]
        except IndexError:
            mo = models.Model(manufacturer=ma, model=row['model'], lifecycle=row['model_lifecycle'], model_type=row['model_type'])
            mo.save()

        try:
            loc = models.Location.objects.filter(name__iexact = row['location_name'], block__iexact = row['location_block'], site__iexact = row['location_site'])[0]
        except IndexError:
            # This should never happen, but abort gracefully if it does
            return render_to_response("assets/import_criticalerrors.html", {'errors':["Line %d: A critical error occurred while attempting to load the location record." % (c.line_num)], 'warnings':[], 'notes':[], 'title':'Bulk Import'})
        
        # Set default values for optional fields if necessary
        try:
            fat = row['finance_asset_tag']
        except KeyError:
            fat = ""

        try:
            pv = row['purchased_value'].strip()
        except KeyError:
            pv = None

        try:
            assigned_user = row['assigned_user']
        except KeyError:
            assigned_user = ""

        try:
            notes = row['notes']
        except KeyError:
            notes = ""

        if row['serial'] == 'Unknown':
            serial = row['serial']
        else:
            serial = row['serial'].upper()

        # Finally, create the asset record
        asset = models.Asset(
                asset_tag = row['asset_tag'].upper(), 
                finance_asset_tag = fat,
                model = mo, 
                status = row['status'].capitalize(), 
                serial = serial,
                date_purchased = datetime.strptime(row['date_purchased'], '%d/%m/%Y'), 
                purchased_value = pv,
                location = loc, 
                assigned_user = assigned_user,
                notes = notes,
            )
        asset.save()

        assets_created.append(asset)

    return render_to_response("assets/import_complete.html", {'record_count':len(assets_created), 'assets_created':assets_created, 'title':'Bulk Import'})
    
def confirm_import(fileobj, request):
    """
    Receives a file object from import_asset, and does validation, shows a
    confirmation/error page and finally does the actual import.
    """

    # Perform validation on the input given to us
    (num_assets, errors, warnings, notes) = validate_import(fileobj)

    # Stop and complain if we've got errors
    if errors:
        return render_to_response("assets/import_criticalerrors.html", {'errors':errors, 'warnings':warnings, 'notes':notes, 'title':'Bulk Import'})

    # Otherwise, render the confirmation page
    return render_to_response("assets/import_confirm.html", {'warnings':warnings, 'notes':notes, 'record_count':num_assets, 'csv':fileobj.read(), 'title':'Bulk Import'}, context_instance=RequestContext(request))

@login_required(redirect_field_name="")
def import_asset(request):
    """
    Displays a form prompting user to upload CSV containing assets. Passes
    uploaded file object to confirm_import to validate data and prompt the user
    for confirmation.
    """
    if request.method == 'POST':
        form = ImportForm(request.POST, request.FILES)
        if form.is_valid():
            return confirm_import(request.FILES['assets_to_import'], request)
    else:
        form = ImportForm()
    return render_to_response("assets/import_intro.html", {'form':form, 'title':'Bulk import'}, context_instance=RequestContext(request))

def recursive_lookup(obj, field):
    """
    Looks up a field__field__filed style reference.
    """
    try:
        return getattr(obj, field)
    except AttributeError:
        if '__' in field:
            (f, rest) = field.split('__', 1)
            return recursive_lookup(getattr(obj, f), rest)
        else:
            raise AttributeError

def build_fields(c=models.Asset, queryset=None):
    """
    Builds a list of fields on the specified Model, recursing into ForeignKey
    relationships as necessary.

    Returns a list of field names. If a queryset is provided to a set of model
    instances, return a list of tuples, where list[0] is a tuple of field names
    and the remaining items are tuples of field values.

    Caveats:
        - Unsure how this handles ManyToMany relationships, YMMV
        - This function probably uses internal django methods
    """

    field_names = []
    for field in c._meta.get_all_field_names():
        # Skip a variety uninteresting field names, from reversion or restless
        if field in ('id', 'created', 'creator', 'modified', 'modifier', ):
            continue

        (f_obj, _model, rel, _m2m) = c._meta.get_field_by_name(field)

        try:
            # Construct django-style relational field names, so we can just
            # dump them into values_list() below to get values
            field_names += ["%s__%s" % (field, x) for x in build_fields(f_obj.rel.to)]
        except AttributeError:
            # Reverse relations are RelatedObjects with no .rel attribute
            #if isinstance(f_obj, RelatedObject):
	    if isinstance(f_obj, ForeignObjectRel):
                continue

            field_names.append(field)

    if queryset:
        #raise Exception(repr(queryset))
        field_data = [field_names]
        # I <3 Python
        field_data += queryset.values_list(*field_names)

        for row in queryset:
            output_row = []
            for field in field_names:
                try:
                    o = unicode(recursive_lookup(row, field))
                    if not o:
                        o = u''
                    output_row.append(o)
                except AttributeError:
                    output_row.append(u'')
            field_data.append(output_row)

        return field_data
    else:
        return field_names

# From http://docs.python.org/library/csv.html#csv-examples
class UnicodeWriter:
    """
    A CSV writer which will write rows to CSV file "f",
    which is encoded in the given encoding.
    """

    def __init__(self, f, dialect=csv.excel, encoding="utf-8", **kwds):
        # Redirect output to a queue
        self.queue = cStringIO.StringIO()
        self.writer = csv.writer(self.queue, dialect=dialect, **kwds)
        self.stream = f
        self.encoder = codecs.getincrementalencoder(encoding)()

    def writerow(self, row):
        self.writer.writerow([unicode(s).encode("utf-8") for s in row])
        # Fetch UTF-8 output from the queue ...
        data = self.queue.getvalue()
        data = data.decode("utf-8")
        # ... and reencode it into the target encoding
        data = self.encoder.encode(data)
        # write to the target stream
        self.stream.write(data)
        # empty queue
        self.queue.truncate(0)

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)

@login_required(redirect_field_name="")
def export(request):
    """
    Dumps assets out as a CSV. Filters may be applied using the following GET
    attributes:

     * manufacturer
     * manufacturer_id
     * model
     * site
     * status
     * asset_tag
     * serial

    """
    response = HttpResponse(mimetype="text/csv")
    #response = HttpResponse(mimetype="text/plain")

    filename = "assets_%s.csv" % (datetime.now().strftime("%Y%m%d"))
    response['Content-Disposition'] = 'attachment; filename=%s' % (filename)

    c = UnicodeWriter(response)

    q = models.Asset.objects

    # Read in filters
    manufacturer = request.GET.get('manufacturer')
    manufacturer_id = request.GET.get('manufacturer_id')
    model = request.GET.get('model')
    site = request.GET.get('site')
    status = request.GET.get('status')
    asset_tag = request.GET.get('asset_tag')
    serial = request.GET.get('serial')

    if manufacturer:
        q = q.filter(model__manufacturer__name__icontains = manufacturer)

    if manufacturer_id:
        q = q.filter(model__manufacturer__id__exact = manufacturer_id)

    if model:
        q = q.filter(model__model__icontains = model)

    if site:
        q = q.filter(location__site__icontains = site)

    if status:
        q = q.filter(status__icontains = status)

    if asset_tag:
        q = q.filter(asset_tag__icontains = asset_tag)

    if serial:
        q = q.filter(serial__icontains = serial)

    if not (manufacturer or model or site or status or asset_tag or serial):
        q = q.all()

    output_rows = build_fields(models.Asset, q)
    if q:
        c.writerows(output_rows)
    else:
        # If there's no data, output_rows will be a single row
        c.writerow(output_rows)

    return response

def categories(request):
    """Prints a list of valid categories for the model_type field.
    """
    l = []
    for (a, b) in models.Model.type_choices:
        l.append(a)

    return render_to_response("assets/categories.html", {'categories': l})
