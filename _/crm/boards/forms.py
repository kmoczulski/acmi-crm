from django import forms

from ..forms import active_users
from ..models import BOARD_COLOURS, Board, Campus, Project
from . import bound, logic

# Record boards: one list per status of the record type, a card for every record (crm/boards/bound.py).
RECORD_TEMPLATES = [(f"record:{k}", f"Record board: {rt.board_name.lower()}") for k, rt in bound.RECORD_TYPES.items()]


class BoardForm(forms.Form):
    name = forms.CharField(max_length=120)
    template = forms.ChoiceField(choices=[(k, v[0]) for k, v in logic.TEMPLATES.items()] + RECORD_TEMPLATES,
                                 initial="project", required=False)
    kind = forms.ChoiceField(choices=[(v, l) for v, l in Board.Kind.choices if v != Board.Kind.BOUND], initial=Board.Kind.PROJECT)
    campus = forms.ModelChoiceField(queryset=Campus.objects.all(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.filter(active=True), required=False)
    colour = forms.ChoiceField(choices=BOARD_COLOURS, initial="navy")
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class BoardSettingsForm(forms.ModelForm):
    class Meta:
        model = Board
        fields = ["name", "description", "colour", "campus", "project"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(active=True)


class MembersForm(forms.Form):
    members = forms.ModelMultipleChoiceField(queryset=active_users(), required=False, widget=forms.CheckboxSelectMultiple)
