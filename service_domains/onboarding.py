"""Plain-language first-use guidance for the four service workspaces."""


_GUIDES = {
    'panel': {
        'title': 'Set up your first Panel hearing',
        'workspace_title': 'Your Panel workspace',
        'summary': ('A hearing keeps its candidates, scoring criteria, panels, and invited '
                    'examiners together from setup through result release.'),
        'create_label': 'Create a hearing',
        'create_anchor': '#create-hearing',
        'resume_label': 'Continue your latest hearing',
        'privacy_note': ('A new hearing stays a draft. Examiners cannot score until you open it, '
                         'and results stay private until you release them.'),
        'steps': (
            ('Create a hearing', 'Name the assessment and choose its result method.'),
            ('Add candidates and criteria', 'Set up who will be assessed and what examiners will score.'),
            ('Invite examiners and open scoring', 'Send private invitations, then open the ready hearing.'),
            ('Review and release results', 'Check compiled results, sign them off, and release them when ready.'),
        ),
    },
    'competition': {
        'title': 'Set up your first competition',
        'workspace_title': 'Your Competition workspace',
        'summary': ('Build the event in order: tracks and entries first, then rounds, judges, '
                    'scoring, advancement, and the final reveal.'),
        'create_label': 'Create a competition',
        'create_anchor': '#create-competition',
        'resume_label': 'Continue your latest competition',
        'privacy_note': ('The public leaderboard remains hidden until you complete the event and '
                         'choose to reveal it.'),
        'steps': (
            ('Create the competition', 'Choose a name and whether audience votes contribute to results.'),
            ('Add tracks and entries', 'Put each entry in a track and choose how entries advance.'),
            ('Create rounds and invite judges', 'Start judging only after the round contains the right entries.'),
            ('Advance, finish, and reveal', 'Review ties and results before making the leaderboard public.'),
        ),
    },
    'team': {
        'title': 'Set up your first Team and Training series',
        'workspace_title': 'Your Team and Training workspace',
        'summary': ('A series reuses a team roster and rubric across demos, then tracks competency '
                    'outcomes over time.'),
        'create_label': 'Create a series',
        'create_anchor': '#create-series',
        'resume_label': 'Continue your latest series',
        'privacy_note': ('Team demos use the shared assessment workspace. Learner outcomes and '
                         'anonymous feedback are managed from the series.'),
        'steps': (
            ('Create a series and add your team', 'Keep the roster in one place for repeated demos.'),
            ('Create a Team demo setup', 'Make a Team session, add its rubric, then save it as a Team template.'),
            ('Add competency criteria', 'Set the evidence and pass score used for each demo.'),
            ('Create each demo from the series', 'The series copies its roster and rubric into a new session.'),
            ('Review trends and certificates', 'Record competency decisions and follow changes across demos.'),
        ),
    },
    'showcase': {
        'title': 'Set up your first Showcase event',
        'workspace_title': 'Your Showcase workspace',
        'summary': ('Prepare exhibits, judge criteria, and judge assignments before opening the '
                    'event for visitor ratings.'),
        'create_label': 'Create an event',
        'create_anchor': '#create-showcase',
        'resume_label': 'Continue your latest event',
        'privacy_note': ('Ratings are accepted only while the event is open. Public results appear '
                         'only after you publish them.'),
        'steps': (
            ('Create the event', 'Set event times and whether minors are participating.'),
            ('Add exhibits and judge criteria', 'Import the exhibit list, then configure how invited judges score.'),
            ('Assign judges and prepare QR labels', 'Assign each judge to exhibits and close any coverage gaps.'),
            ('Confirm consent, open, and publish', 'Record required consent before opening; publish results when ready.'),
        ),
    },
}


def service_onboarding(service, *, resume_url='', resume_name=''):
    """Return display-only guidance and a safe resume link for a service dashboard."""
    guide = _GUIDES[service]
    has_existing_work = bool(resume_url)
    return {
        'slug': service,
        'title': guide['workspace_title'] if has_existing_work else guide['title'],
        'kicker': 'Your workflow' if has_existing_work else 'Getting started',
        'summary': guide['summary'],
        'create_label': guide['create_label'],
        'create_anchor': guide['create_anchor'],
        'resume_label': guide['resume_label'],
        'resume_url': resume_url,
        'resume_name': resume_name,
        'privacy_note': guide['privacy_note'],
        'steps': guide['steps'],
    }
