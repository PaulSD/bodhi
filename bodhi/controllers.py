# $Id: controllers.py,v 1.11 2007/01/08 06:07:07 lmacken Exp $
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import rpm
import mail
import logging
import cherrypy

from koji import GenericError
from datetime import datetime
from sqlobject import SQLObjectNotFound
from sqlobject.sqlbuilder import AND, OR

from turbogears import (controllers, expose, validate, redirect, identity,
                        paginate, flash, error_handler, validators, config, url)
from turbogears.widgets import DataGrid

from bodhi import buildsys, util
from bodhi.rss import Feed
from bodhi.util import flash_log, get_pkg_pushers
from bodhi.new import NewUpdateController, update_form
from bodhi.admin import AdminController
from bodhi.metrics import Metrics
from bodhi.model import (Package, PackageBuild, PackageUpdate, Release,
                         Bugzilla, CVE, Comment)
from bodhi.search import SearchController
from bodhi.widgets import CommentForm, OkCancelForm, CommentCaptchaForm
from bodhi.exceptions import (DuplicateEntryError,
                              PostgresIntegrityError, SQLiteIntegrityError)

log = logging.getLogger(__name__)


class Root(controllers.RootController):

    new = NewUpdateController()
    admin = AdminController()
    search = SearchController()
    rss = Feed("rss2.0")
    metrics = Metrics()

    comment_form = CommentForm()
    comment_captcha_form = CommentCaptchaForm()
    ok_cancel_form = OkCancelForm()

    def exception(self, tg_exceptions=None):
        """ Generic exception handler """
        log.error("Exception thrown: %s" % str(tg_exceptions))
        flash_log(str(tg_exceptions))
        if 'tg_format' in cherrypy.request.params and \
                cherrypy.request.params['tg_format'] == 'json':
            return dict()
        raise redirect("/")

    def jsonRequest(self):
        return 'tg_format' in cherrypy.request.params and \
                cherrypy.request.params['tg_format'] == 'json'

    @expose(template='bodhi.templates.welcome')
    def index(self):
        """
        The main dashboard.  Here we generate the DataGrids for My Updates and 
        the latest comments.
        """
        from bodhi.util import make_update_link, make_type_icon, make_karma_icon
        from bodhi.util import make_request_icon
        RESULTS, FIELDS, GRID = range(3)
        updates = None

        # { 'Title' : [SelectResults, [(row, row_callback),]], ... }
        grids = {
            'comments' : [
                Comment.select(orderBy=Comment.q.timestamp).reversed(),
                [
                    ('Update', make_update_link),
                    ('Comment', lambda row: row.text),
                    ('From', lambda row: row.author),
                    ('Karma', make_karma_icon)
                ]
            ],
       }

        if identity.current.anonymous:
            updates = 'latest'
            grids['latest'] = [
                PackageUpdate.select(
                    orderBy=PackageUpdate.q.date_submitted
                ).reversed(),
                [
                    ('Update', make_update_link),
                    ('Release', lambda row: row.release.long_name),
                    ('Status', lambda row: row.status),
                    ('Type', make_type_icon),
                    ('Request', make_request_icon),
                    ('Karma', make_karma_icon),
                    ('Submitter', lambda row: row.submitter),
                    ('Age', lambda row: row.get_submitted_age()),
                ]
            ]
        else:
            updates = 'mine'
            grids['mine'] = [
                PackageUpdate.select(
                    PackageUpdate.q.submitter == identity.current.user_name,
                    orderBy=PackageUpdate.q.date_pushed
                ).reversed(),
                [
                    ('Update', make_update_link),
                    ('Release', lambda row: row.release.long_name),
                    ('Status', lambda row: row.status),
                    ('Type', make_type_icon),
                    ('Request', make_request_icon),
                    ('Karma', make_karma_icon),
                    ('Age', lambda row: row.get_submitted_age()),
                ]
            ]

        for key, value in grids.items():
            if not value[RESULTS].count():
                grids[key].append(None)
                continue
            if value[RESULTS].count() > 5:
                value[RESULTS] = value[RESULTS][:5]
            value[RESULTS] = list(value[RESULTS])
            grids[key].append(DataGrid(fields=value[FIELDS],
                                       default=value[RESULTS]))

        return dict(now=datetime.utcnow(), updates=grids[updates][GRID],
                    comments=grids['comments'][GRID])

    @expose(template='bodhi.templates.pkgs')
    def pkgs(self):
        return dict()

    @expose(template="bodhi.templates.login", allow_json=True)
    def login(self, forward_url=None, previous_url=None, *args, **kw):
        if not identity.current.anonymous and identity.was_login_attempted() \
           and not identity.get_identity_errors():
            if self.jsonRequest():
                return dict(user=identity.current.user)
            raise redirect(forward_url)

        forward_url=None
        previous_url= cherrypy.request.path

        if identity.was_login_attempted():
            msg=_("The credentials you supplied were not correct or "
                  "did not grant access to this resource.")
        elif identity.get_identity_errors():
            msg=_("You must provide your credentials before accessing "
                  "this resource.")
        else:
            msg=_("Please log in.")
            forward_url= cherrypy.request.headers.get("Referer", "/")

        cherrypy.response.status=403
        return dict(message=msg, previous_url=previous_url, logging_in=True,
                    original_parameters=cherrypy.request.params,
                    forward_url=forward_url)
    @expose()
    def logout(self):
        identity.current.logout()
        raise redirect('/')

    @expose(template="bodhi.templates.list", allow_json=True)
    @paginate('updates', limit=20, allow_limit_override=True)
    def list(self, release=None, bugs=None, cves=None, status=None, type=None,
             package=None, mine=False):
        """ Return a list of updates based on given parameters """
        log.debug("list(%s, %s, %s, %s, %s, %s, %s)" % (release, bugs, cves,
                  status, type, package, mine))
        query = []
        updates = []

        try:
            if release:
                rel = Release.byName(release.upper())
                query.append(PackageUpdate.q.releaseID == rel.id)
            if status:
                query.append(PackageUpdate.q.status == status)
            if type:
                query.append(PackageUpdate.q.type == type)
            if mine:
                query.append(
                    PackageUpdate.q.submitter == identity.current.user_name)

            updates = PackageUpdate.select(AND(*query))

            # The package argument may be an update, build or package.
            if package:
                try:
                    update = PackageUpdate.byTitle(package)
                    if not release and not status and not type:
                        updates = [update]
                    else:
                        if update in updates:
                            updates = [update] # There can be only one
                        else:
                            updates = []
                except SQLObjectNotFound:
                    try:
                        pkg = Package.byName(package)
                        if not release and not status and not type:
                            updates = pkg.updates()
                        else:
                            updates = filter(lambda up: up in updates,
                                             pkg.updates())
                    except SQLObjectNotFound:
                        try:
                            build = PackageBuild.byNvr(package)
                            if not release and not status and not type:
                                updates = [build.update,]
                            else:
                                results = []
                                for update in updates:
                                    if build in update.builds:
                                        results.append(update)
                                updates = results
                        except SQLObjectNotFound:
                            updates = []

            # Filter results by Bugs and/or CVEs
            if bugs:
                results = []
                for bug in map(Bugzilla.byBz_id, map(int, bugs.split(','))):
                    map(results.append,
                        filter(lambda x: bug in x.bugs, updates))
                updates = results
            if cves:
                results = []
                for cve in map(CVE.byCve_id, cves.split(',')):
                    map(results.append,
                        filter(lambda x: cve in x.cves, updates))
                updates = results
        except SQLObjectNotFound, e:
            flash_log(e)
            if self.jsonRequest():
                return dict(updates=[])

        # If we're called via JSON, then simply return the PackageUpdate.__str__
        # else, we return a list of PackageUpdate objects to our template
        if self.jsonRequest():
            updates = map(unicode, updates)

        if isinstance(updates, list): num_items = len(updates)
        else: num_items = updates.count()

        return dict(updates=updates, num_items=num_items,
                    title="%d updates found" % num_items)

    @expose(template="bodhi.templates.mine", allow_json=True)
    @identity.require(identity.not_anonymous())
    @paginate('updates', limit=20, allow_limit_override=True)
    def mine(self):
        """ List all updates submitted by the current user """
        updates = PackageUpdate.select(
                    OR(PackageUpdate.q.submitter == util.displayname(identity),
                       PackageUpdate.q.submitter == identity.current.user_name),
                    orderBy=PackageUpdate.q.date_pushed).reversed()
        return dict(updates=self.jsonRequest() and map(unicode, updates) or
                    updates, title='%s\'s updates' % identity.current.user_name,
                    num_items=updates.count())

    @expose(allow_json=True)
    @identity.require(identity.not_anonymous())
    def request(self, action, update):
        """
        Request that a specified action be performed on a given update.
        Action must be one of: 'testing', 'stable', 'unpush' or 'obsolete'.
        """
        log.debug("request(%s, %s)" % (action, update))
        try:
            update = PackageUpdate.byTitle(update)
        except SQLObjectNotFound:
            flash_log("Cannot find update %s for action: %s" % (update, action))
            if self.jsonRequest(): return dict()
            raise redirect('/')
        if not util.authorized_user(update, identity):
            flash_log("Unauthorized to perform action on %s" % update.title)
            if self.jsonRequest(): return dict()
            raise redirect(update.get_url())
        if action == update.status:
            flash_log("%s already %s" % (update.title, action))
            if self.jsonRequest: return dict()
            raise redirect(update.get_url())
        if action == update.request:
            flash_log("%s has already been submitted to %s" % (update.title,
                                                               update.request))
            if self.jsonRequest: return dict()
            raise redirect(update.get_url())
        if action == 'unpush':
            update.unpush()
            flash_log("%s has been unpushed" % update.title)
            if self.jsonRequest(): return dict(update=unicode(update))
            raise redirect(update.get_url())
        if action == 'obsolete':
            update.obsolete()
            flash_log("%s has been obsoleted" % update.title)
            if self.jsonRequest(): return dict(update=unicode(update))
            raise redirect(update.get_url())
        if action not in ('testing', 'stable', 'obsolete'):
            flash_log("Unknown request: %s" % action)
            if self.jsonRequest(): return dict()
            raise redirect(update.get_url())
        if action == 'stable' and update.type == 'security' and \
           not update.approved:
            flash_log("%s will be pushed to testing while it awaits approval "
                      "of the Security Team" % update.title)
            update.request = 'testing'
            mail.send(config.get('security_team'), 'security', update)
            if self.jsonRequest(): return dict()
            raise redirect(update.get_url())

        update.request = action
        update.pushed = False
        update.date_pushed = None
        flash_log("%s has been submitted for %s" % (update.title, action))
        mail.send_admin(action, update)
        if self.jsonRequest(): return dict()
        raise redirect(update.get_url())

    @expose()
    @identity.require(identity.not_anonymous())
    def revoke(self, update):
        """ Revoke a push request for a specified update """
        update = PackageUpdate.byTitle(update)
        if not util.authorized_user(update, identity):
            flash_log("Unauthorized to revoke request for %s" % update.title)
            raise redirect(update.get_url())
        flash_log("%s request revoked" % update.request.title())
        mail.send_admin('revoke', update)
        update.request = None
        raise redirect(update.get_url())

    @expose(allow_json=True)
    @identity.require(identity.not_anonymous())
    def delete(self, update):
        """ Delete a pending update """
        try:
            update = PackageUpdate.byTitle(update)
            if not util.authorized_user(update, identity):
                flash_log("Cannot delete an update you did not submit")
                if self.jsonRequest(): return dict()
                raise redirect(update.get_url())
            if not update.pushed:
                mail.send_admin('deleted', update)
                msg = "Deleted %s" % update.title
                map(lambda x: x.destroySelf(), update.comments)
                map(lambda x: x.destroySelf(), update.builds)
                update.destroySelf()
                flash_log(msg)
            else:
                flash_log("Cannot delete a pushed update")
        except SQLObjectNotFound:
            flash_log("Update %s does not exist" % update)
        if self.jsonRequest(): return dict()
        raise redirect("/")

    @identity.require(identity.not_anonymous())
    @expose(template='bodhi.templates.form')
    def edit(self, update):
        """ Edit an update """
        update = PackageUpdate.byTitle(update)
        if not util.authorized_user(update, identity):
            flash_log("Cannot edit an update you did not submit")
            raise redirect(update.get_url())
        values = {
                'builds'    : {'text':update.title, 'hidden':update.title},
                'release'   : update.release.long_name,
                'testing'   : update.status == 'testing',
                'request'   : str(update.request).title(),
                'type'      : update.type,
                'notes'     : update.notes,
                'bugs'      : update.get_bugstring(),
                'edited'    : update.title,
        }
        if update.status == 'testing':
            flash("Editing this update will move it back to a pending state.")
        return dict(form=update_form, values=values, action=url("/save"),
                    title='Edit Update')

    @expose(allow_json=True)
    @error_handler(new.index)
    @validate(form=update_form)
    @identity.require(identity.not_anonymous())
    def save(self, builds, release, type, notes, bugs, close_bugs=False,
             edited=False, request='testing', suggest_reboot=False, **kw):
        """
        Save an update.  This includes new updates and edited.
        """
        log.debug("save(%s, %s, %s, %s, %s, %s, %s, %s)" % (builds, release,
            type, notes, bugs, close_bugs, edited, kw))

        note = []
        update_builds = []
        if not bugs: bugs = []
        release = Release.select(
                        OR(Release.q.long_name == release,
                           Release.q.name == release))[0]

        # Parameters used to re-populate the update form if something fails
        params = {
                'builds.text' : ' '.join(builds),
                'release'     : release.long_name,
                'type'        : type,
                'bugs'        : ' '.join(map(str, bugs)),
                'notes'       : notes,
                'edited'      : edited,
                'close_bugs'  : close_bugs and 'True' or '',
        }

        if edited:
            try:
                edited = PackageUpdate.byTitle(edited)
            except SQLObjectNotFound:
                flash_log("Cannot find update '%s' to edit" % edited)
                raise redirect('/new', **params)

        # Make sure the submitter has commit access to these builds
        for build in builds:
            nvr = util.get_nvr(build)
            people = None
            groups = None
            try:
                people, groups = get_pkg_pushers(nvr[0],
                                        release.long_name.split()[0],
                                        release.long_name[-1])
            except Exception, e:
                flash_log(e)
                if self.jsonRequest():
                    return dict()
                raise redirect('/new', **params)
            if not identity.current.user_name in people[0] and \
               not 'releng' in identity.current.groups and \
               not 'security_respons' in identity.current.groups and \
               not 'cvsadmin' in identity.current.groups and \
               not filter(lambda x: x in identity.current.groups, groups[0]):
                flash_log("%s does not have commit access to %s" % (
                          identity.current.user_name, nvr[0]))
                if self.jsonRequest(): return dict()
                raise redirect('/new', **params)

        # Iterate over all of the builds, making sure they are all tagged
        # appropriately in koji
        koji = buildsys.get_session()
        for build in builds:
            log.debug("Validating koji tag for %s" % build)
            candidate = '%s-updates-candidate' % release.dist_tag
            try:
                tags = [tag['name'] for tag in koji.listTags(build)]
                if edited:
                    if build in edited.title:
                        if edited.get_build_tag() not in tags:
                            flash_log("%s not tagged with %s" % (edited.title,
                                      edited.get_build_tag()))
                            if self.jsonRequest(): return dict()
                            raise redirect('/new', **params)
                    else: # new build
                        if candidate not in tags:
                            flash_log("%s not tagged with %s" % (build,
                                      candidate))
                            if self.jsonRequest(): return dict()
                            raise redirect('/new', **params)
                else:
                    if candidate not in tags:
                        flash_log("%s not tagged with %s" % (build, candidate))
                        if self.jsonRequest(): return dict()
                        raise redirect('/new', **params)
            except GenericError, e:
                flash_log("Invalid build: %s" % build)
                if self.jsonRequest(): return dict()
                raise redirect('/new', **params)

            # Check for broken update paths against all previous releases
            kojiBuild = koji.getBuild(build)
            kojiBuild['nvr'] = "%s-%s-%s" % (kojiBuild['name'],
                                             kojiBuild['version'],
                                             kojiBuild['release'])
            tag = release.dist_tag
            nvr = util.get_nvr(build)
            while True:
                try:
                    for kojiTag in (tag, tag + '-updates'):
                        log.debug("Checking for broken update paths in " + kojiTag)
                        for oldBuild in koji.listTagged(kojiTag,package=nvr[0]):
                            if rpm.labelCompare(util.build_evr(kojiBuild),
                                                util.build_evr(oldBuild)) < 0:
                                flash_log("Broken update path: %s is older "
                                          "than %s in %s" % (kojiBuild['nvr'],
                                          oldBuild['nvr'], kojiTag))
                                raise redirect('/new', **params)
                except GenericError:
                    break

                # Check against the previous release (until one doesn't exist)
                tag = tag[:-1] + str(int(tag[-1]) - 1)

        # If we're editing a testing update, unpush it first.  Then destroy
        # all associated builds, as they will be re-created in the next step.
        if edited:
            update = edited
            if update.status == 'testing':
                update.unpush()
            elif update.status == 'stable':
                flash_log("Cannot edit stable updates")
                if self.jsonRequest(): return dict()
                raise redirect('/new', **params)
            for build in update.builds:
                build.destroySelf()

        # Create all of the PackageBuild and PackageUpdate objects
        for build in builds:
            nvr = util.get_nvr(build)
            try:
                package = Package.byName(nvr[0])
            except SQLObjectNotFound:
                package = Package(name=nvr[0])
            if suggest_reboot:
                package.suggest_reboot = True
            package.committers = people[0] # Update our ACL cache for this pkg

            try:
                pkgBuild = PackageBuild(nvr=build, package=package)
                update_builds.append(pkgBuild)
            except (PostgresIntegrityError, SQLiteIntegrityError,
                    DuplicateEntryError):
                flash_log("Update for %s already exists" % build)
                map(lambda build: build.destroySelf(), update_builds)
                if self.jsonRequest(): return dict()
                raise redirect('/new', **params)

            # Obsolete any older pending/testing updates
            for oldBuild in package.builds:
                if oldBuild.update and \
                   oldBuild.update.status in ('pending', 'testing'):
                    if release != oldBuild.update.release:
                        log.debug("Skipping obsoleting %s" % oldBuild.nvr)
                        continue
                    if oldBuild.update.request:
                        # Skip obsoleting updates that are headed somewhere
                        continue 
                    if rpm.labelCompare(util.get_nvr(oldBuild.nvr), nvr) < 0:
                        log.debug("Obsoleting %s" % oldBuild.nvr)
                        for bug in oldBuild.update.bugs:
                            bugs.append(unicode(bug.bz_id))
                        oldBuild.update.obsolete(newer=build)
                        note.append('This update has obsoleted %s'%oldBuild.nvr)

        # Modify or create the PackageUpdate
        if edited:
            p = edited
            p.set(release=release, date_modified=datetime.utcnow(),
                  notes=notes, type=type, title=','.join(builds),
                  close_bugs=close_bugs)
            log.debug("Edited update %s" % edited.title)
        else:
            try:
                p = PackageUpdate(title=','.join(builds), release=release,
                                  submitter=identity.current.user_name,
                                  notes=notes, type=type, close_bugs=close_bugs)
                log.info("Adding new update %s" % builds)
            except (PostgresIntegrityError, SQLiteIntegrityError,
                    DuplicateEntryError):
                flash_log("Update for %s already exists" % builds)
                if self.jsonRequest(): return dict()
                raise redirect('/new', **params)

        # Add the PackageBuilds to our PackageUpdate
        for build in update_builds:
            build.update = p

        # Add/remove the necessary Bugzillas
        p.update_bugs(bugs)

        # If there are any security bugs, make sure this update is
        # marked as security
        if p.type != 'security':
            for bug in p.bugs:
                if bug.security:
                    p.type = 'security'
                    break

        if edited:
            mail.send(p.submitter, 'edited', p)
            note.insert(0, "Update successfully edited")
        else:
            # Notify security team of newly submitted security updates
            if p.type == 'security':
                mail.send(config.get('security_team'), 'security', p)
            mail.send(p.submitter, 'new', p)
            note.insert(0, "Update successfully created")

            # Comment on all bugs
            for bug in p.bugs:
                bug.add_comment(p, "%s has been submitted as an update "
                                "for %s" % (p.title, p.release.long_name))

        # If a request is specified, make it.  By default we're submitting new
        # updates directly into testing
        if request and request != "None" and request != p.request:
            self.request(request.lower(), p.title)

        flash_log('. '.join(note))

        # For command line submissions, return PackageUpdate.__str__()
        if self.jsonRequest():
            return dict(update=unicode(p))

        raise redirect(p.get_url())

    @expose(template='bodhi.templates.list')
    @paginate('updates', limit=20, allow_limit_override=True)
    def default(self, *args, **kw):
        """
        This method allows for the following requests

            /release/status/update
            /release/security
            /release/update_id
            /packagename
        """
        args = list(args)
        status = 'stable'
        order = PackageUpdate.q.date_pushed
        template = 'bodhi.templates.list'
        release = None
        single = None
        query = []

        # /Package.name
        if len(args) == 1:
            try:
                package = Package.byName(args[0])
                return dict(tg_template='bodhi.templates.pkg', pkg=package,
                            updates=[])
            except SQLObjectNotFound:
                pass

        # /Release.name
        try:
            release = Release.byName(args[0])
            query.append(PackageUpdate.q.releaseID == release.id)
            del args[0]
        except SQLObjectNotFound:
            pass

        # /Release.name/{PackageUpdate.update_id,PackageUpdate.status}
        if len(args):
            if args[0] in ('testing', 'stable', 'pending', 'obsolete'):
                if args[0] == 'testing':
                    template = 'bodhi.templates.testing'
                elif args[0] == 'pending':
                    template = 'bodhi.templates.pending'
                    order = PackageUpdate.q.date_submitted
                status = args[0]
                query.append(PackageUpdate.q.status == status)
            elif args[0] == 'security':
                query.append(PackageUpdate.q.type == 'security')
                query.append(PackageUpdate.q.pushed == True)
                query.append(PackageUpdate.q.status == status)
                status = 'security'
            else:
                query.append(PackageUpdate.q.update_id == args[0])
                single = True
            del args[0]
        else:
            query.append(PackageUpdate.q.status == status)

        # /Release.name/PackageUpdate.status/PackageUpdate.title
        if len(args):
            query.append(PackageUpdate.q.title == args[0])
            single = args[0]
            del args[0]

        # Run the query that we just built
        updates = PackageUpdate.select(AND(*query), orderBy=order).reversed()

        num_updates = updates.count()
        if num_updates and (num_updates == 1 or single):
            update = updates[0]
            update.comments.sort(lambda x, y: cmp(x.timestamp, y.timestamp))
            form = identity.current.anonymous and self.comment_captcha_form \
                    or self.comment_form
            return dict(tg_template='bodhi.templates.show', update=update,
                        updates=[], comment_form=form,
                        values={'title' : update.title})
        elif num_updates > 1:
            try:
                return dict(tg_template=template, updates=updates,
                            num_items=num_updates, title='%s %s Updates' % (
                            release.long_name, status.title()))
            except AttributeError:
                pass
        elif single and num_updates == 0:
            # A single update was specified, but not found.  Be nice and
            # attempt to find the update that the user is looking for and 
            # redirect them to it.  (Bug #426941)
            try:
                update = PackageUpdate.byTitle(single)
                raise redirect(update.get_url())
            except SQLObjectNotFound:
                pass
        else:
            return dict(tg_template=template, updates=[], num_items=0,
                        title='No updates found')

        flash_log("The path %s cannot be found" % cherrypy.request.path)
        raise redirect("/")

    @expose(template='bodhi.templates.show')
    @validate(form=comment_captcha_form)
    @validate(validators={ 'karma' : validators.Int() })
    def captcha_comment(self, text, title, author, karma, captcha={},
                        tg_errors=None):
        try:
            update = PackageUpdate.byTitle(title)
        except SQLObjectNotFound:
            flash_log("Update %s does not exist" % title)
        if tg_errors:
            if tg_errors.has_key('text') or tg_errors.has_key('author'):
                flash_log("Please fill in all comment fields")
            flash_log(tg_errors)
            return dict(update=update, updates=[], 
                        values={'title':update.title},
                        comment_form=self.comment_captcha_form)
        elif karma not in (0, 1, -1):
            flash_log("Karma must be one of (1, 0, -1)")
            return dict(update=update, updates=[],
                        values={'title' : update.title},
                        comment_form=self.comment_captcha_form)
        if text == 'None': text = None
        update.comment(text, karma, author=author, anonymous=True)
        raise redirect(update.get_url())

    @expose(allow_json=True)
    @error_handler()
    @validate(form=comment_form)
    @validate(validators={ 'karma' : validators.Int() })
    @identity.require(identity.not_anonymous())
    def comment(self, text, title, karma, tg_errors=None):
        if tg_errors:
            flash_log(tg_errors)
        elif karma not in (0, 1, -1):
            flash_log("Karma must be one of (1, 0, -1)")
        else:
            try:
                update = PackageUpdate.byTitle(title)
                if text == 'None': text = None
                update.comment(text, karma)
                if self.jsonRequest(): return dict(update=unicode(update))
                raise redirect(update.get_url())
            except SQLObjectNotFound:
                flash_log("Update %s does not exist" % title)
        if self.jsonRequest(): return dict()
        raise redirect('/')

    @expose(template='bodhi.templates.comments')
    @paginate('comments', limit=20, allow_limit_override=True)
    def comments(self):
        data = Comment.select(Comment.q.author != 'bodhi',
                              orderBy=Comment.q.timestamp).reversed()
        return dict(comments=data, num_items=data.count())

    @expose(template='bodhi.templates.confirmation')
    @identity.require(identity.not_anonymous())
    def confirm_delete(self, nvr=None, ok=None, cancel=None):
        update = PackageUpdate.byTitle(nvr)
        if ok:
            flash(_(u"Delete completed"))
            raise redirect('/delete/%s' % update.title)
        if cancel:
            flash(_(u"Delete canceled" ))
            raise redirect(update.get_url())
        return dict(form=self.ok_cancel_form, nvr=nvr)

    @expose(template='bodhi.templates.obsolete')
    def obsolete_dialog(self, update):
        from bodhi.widgets import ObsoleteForm
        package = Package.byName('-'.join(update.split('-')[:-2]))
        builds = filter(lambda x: x.update.status in ('testing', 'pending'),
                        package.builds)
        if not len(builds):
            return dict(dialog=None)
        return dict(dialog=ObsoleteForm(builds))

    @expose("json")
    def obsolete(self, updates, *args, **kw):
        """
        Called by our ObsoleteForm widget.  This method will
        request that any specified updates be marked as obsolete
        """
        log.debug("obsolete(%s, %s, %s)" % (updates, args, kw))
        errors = []
        if type(updates) != list:
            updates = [updates]
        for update in updates:
            up = PackageBuild.byNvr(update).update
            if not util.authorized_user(up, identity):
                msg = "Unauthorized to obsolete %s" % up.title
                errors.append(msg)
                flash_log(msg)
            else:
                up.obsolete()
        return len(errors) and errors[0] or "Done!"

    @expose(allow_json=True)
    def dist_tags(self):
        return dict(tags=[r.dist_tag for r in Release.select()])

    @expose(allow_json=True)
    @identity.require(identity.in_group("security_respons"))
    def approve(self, update):
        """
        Security response team approval for pending security updates
        """
        try:
            update = PackageUpdate.byTitle(update)
        except SQLObjectNotFound:
            flash_log("%s not found" % update)
            if self.jsonRequest(): return dict()
            raise redirect('/')
        update.approved = datetime.utcnow()
        update.request = 'stable'
        flash_log("%s has been approved and submitted for pushing to stable" %
                  update.title)
        raise redirect(update.get_url())

    @expose(template="bodhi.templates.security")
    @identity.require(identity.in_group("security_respons"))
    def security(self):
        """ Return a list of security updates pending approval """
        updates = PackageUpdate.select(
                    AND(PackageUpdate.q.type == 'security',
                        PackageUpdate.q.status == 'pending',
                        PackageUpdate.q.approved == None))
        return dict(updates=updates)
