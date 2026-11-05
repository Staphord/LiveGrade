from django import forms


class ServiceInterestForm(forms.Form):
    email = forms.EmailField(
        max_length=254, widget=forms.EmailInput(attrs={'class': 'form-control', 'autocomplete': 'email'}))
    consent = forms.BooleanField(label='Email me once when this service opens.')

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()
