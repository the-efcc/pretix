#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020  Raphael Michel and contributors
# Copyright (C) 2020-today pretix GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
import datetime

import pytest
from django.core import mail
from django_scopes import scopes_disabled

from pretix.base.models import Event, Organizer, Team, User


@pytest.fixture
def env():
    with scopes_disabled():
        user = User.objects.create_user('dummy@dummy.dummy', 'dummy')
        orga = Organizer.objects.create(name='CCC', slug='ccc')
        event = Event.objects.create(
            organizer=orga, name='30C3', slug='30c3',
            date_from=datetime.datetime(2013, 12, 26, tzinfo=datetime.timezone.utc),
        )
        team = Team.objects.create(organizer=orga, all_event_permissions=True)
        team.members.add(user)
        team.limit_events.add(event)
    return user, orga, event


@pytest.mark.django_db
def test_send_test_email(client, env):
    user, orga, event = env
    client.login(email='dummy@dummy.dummy', password='dummy')
    mail.outbox = []
    r = client.post('/control/event/{}/{}/settings/email'.format(orga.slug, event.slug), {
        'test_email': 'true',
    }, follow=True)
    assert r.status_code == 200
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [user.email]
