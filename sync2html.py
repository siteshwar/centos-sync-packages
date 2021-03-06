#! /usr/bin/python3

"""Sync packages from a dist-git repository to a koji build system.
Eg. Build packages which are newer in git than in the tag/compose we are
looking at.
"""

from __future__ import print_function

import koji
import json
import sys
import os
import shutil
import tempfile
import spkg
import time
import matchlist
from optparse import OptionParser
import git

if not hasattr(tempfile, 'TemporaryDirectory'):
    class TemporaryDirectory(object):
        """Do it using __del__ as a hack """

        def __init__(self, suffix='', prefix='tmp', dir=None):
            self.name = tempfile.mkdtemp(suffix, prefix, dir)

        def __del__(self):
            shutil.rmtree(self.name)
    tempfile.TemporaryDirectory = TemporaryDirectory

ml_pkgdeny = matchlist.Matchlist()
ml_gitdeny = matchlist.Matchlist()
def load_package_denylist():
    ml_gitdeny.load("conf/sync2build-gittags-denylist.txt")
    ml_pkgdeny.load("conf/sync2build-packages-denylist.txt")


_koji_max_query = 2000
def koji_archpkgs2sigs(kapi, pkgs):
    if len(pkgs) > _koji_max_query:
        for i in range(0, len(pkgs), _koji_max_query):  
            koji_archpkgs2sigs(kapi, pkgs[i:i + _koji_max_query])
        return

    # Get unsigned packages
    kapi.multicall = True
    # Query for the specific key we're looking for, no results means
    # that it isn't signed and thus add it to the unsigned list
    for pkg in pkgs:
        kapi.queryRPMSigs(rpm_id=pkg._koji_rpm_id)

    results = kapi.multiCall()
    for ([result], pkg) in zip(results, pkgs):
        pkg.signed = []
        for res in result:
            if not res['sigkey']:
                continue
            pkg.signed.append(res['sigkey'])
        if len(pkg.signed) == 0:
            pkg.signed = ''
        if len(pkg.signed) == 1:
            pkg.signed = pkg.signed[0]

def koji_pkgs2archsigs(kapi, pkgs):
    if len(pkgs) > _koji_max_query:
        ret = []
        for i in range(0, len(pkgs), _koji_max_query):
            ret.extend(koji_pkgs2archsigs(kapi, pkgs[i:i + _koji_max_query]))
        return ret

    kapi.multicall = True
    for pkg in pkgs:
        kapi.listRPMs(buildID=pkg._koji_build_id)

    ret = []
    results = kapi.multiCall()
    for ([rpms], bpkg) in zip(results, pkgs):
        for rpm in rpms:
            epoch = spkg.epochnum2epoch(rpm['epoch'])
            pkg = spkg.nvr2pkg(rpm['nvr'], epoch=epoch)
            pkg.arch = rpm['arch']
            pkg._koji_rpm_id = rpm['id']
            pkg._koji_build_id = bpkg._koji_build_id
            ret.append(pkg)

    koji_archpkgs2sigs(kapi, ret)
    return ret

def _task_state(info):
    return koji.TASK_STATES[info['state']]

def _pkg_koji_task_state(self):
    if not hasattr(self, '_cached_koji_task_state'):
        tinfo = self._kapi.getTaskInfo(self._koji_task_id)
        # This overwrites the property call
        self._cached_koji_task_state = _task_state(tinfo)
        del self._kapi
    return self._cached_koji_task_state
# This is a hack, so we can continue to use spkg.Pkg() indirectly. Sigh.
spkg.Pkg._koji_task_state = property(_pkg_koji_task_state)

def _koji_buildinfo2pkg(kapi, binfo):
    epoch = spkg.epochnum2epoch(binfo['epoch'])
    pkg = spkg.nvr2pkg(binfo['nvr'], epoch=epoch)
    pkg._koji_build_id = binfo['build_id']
    if 'task_id' in binfo:
        pkg._koji_task_id = binfo['task_id']
        pkg._kapi = kapi
    return pkg

def koji_tag2pkgs(kapi, tag):
    """
    Return a list of latest build packages that are tagged with certain tag
    """
    ret = []
    for rpminfo in kapi.listTagged(tag, inherit=True, latest=True):
        pkg = _koji_buildinfo2pkg(kapi, rpminfo)
        ret.append(pkg)

    return ret

def koji_pkgid2pkgs(kapi, pkgid):
    """
    Return a the build pacakges from a package id
    """
    ret = []
    for binfo in kapi.listBuilds(packageID=pkgid):
        pkg = _koji_buildinfo2pkg(kapi, binfo)
        ret.append(pkg)
    return ret

def _koji_pkg2task_state(kapi, pkg):
    pkgid = kapi.getPackageID(pkg.name)
    for ppkg in koji_pkgid2pkgs(kapi, pkgid):
        if ppkg == pkg:
            return ppkg._koji_task_id, ppkg._koji_task_state
    return None, 'NONE'

def composed_url2pkgs(baseurl):
    """
    Return a list of latest packages that are in the given compose
    """
    import compose

    c = compose.Compose(baseurl)
    cid = c.data_id()
    cstat = c.data_status()
    pdata = c.json_rpms()
    p = compose.packages_from_compose(pdata)
    pb = compose.packages_bin_from_compose(pdata)
    return p, pb, cid, cstat

def composed_url2modules(baseurl):
    """
    Return a list of latest modules that are in the given compose
    """
    import compose

    c = compose.Compose(baseurl)
    cid = c.data_id()
    cstat = c.data_status()
    print('Mod Compose:', cid)
    print(' Status:', cstat)
    mdata = c.json_modules()
    m = compose.modules_from_compose(mdata)
    return compose.dedup_modules(m)

def bpkg2git_tags(bpkg, codir, T="rpms"):
    giturl = "https://git.centos.org/"
    giturl += T
    giturl += "/"
    giturl += bpkg.name
    giturl += ".git"
    try:
        repo = git.Repo.clone_from(giturl, codir)
        tags = repo.tags
    except git.exc.GitCommandError:
        # This means the clone didn't work, so it's a new package.
        tags = []
    return tags

def _tags2pkgs(tags):
    tpkgs = []
    for tag in tags:
        stag = str(tag)
        if not stag.startswith("imports/c8"):
            continue
        stag = stag[len("imports/c8"):]
        # Eg. See: https://git.centos.org/rpms/ongres-scram/releases
        stag = stag.replace('%7e', '~')
        if '%' in stag: # FIXME? panic?
            continue
        if stag.startswith("s/"):
            stream = True
            stag = stag[len("s/"):]
        elif  stag.startswith("/"):
            stream = False
            stag = stag[len("/"):]
        else:
            continue

        # Tag is now N-V-R
        pkg = spkg.nvr2pkg(stag)
        pkg.stream = stream
        tpkgs.append(pkg)

    return tpkgs

# See: https://codepen.io/nathancockerill/pen/OQyXWb
html_header = """\
    <html>
        <head>
        <link rel="dns-prefetch" href="https://fonts.googleapis.com">
            <style>
@import url('https://fonts.googleapis.com/css?family=Source+Sans+Pro:400,700');

$base-spacing-unit: 24px;
$half-spacing-unit: $base-spacing-unit / 2;

$color-alpha: #1772FF;
$color-form-highlight: #EEEEEE;

*, *:before, *:after {
	box-sizing:border-box;
}

body {
	padding:$base-spacing-unit;
	font-family:'Source Sans Pro', sans-serif;
	margin:0;
}

h1,h2,h3,h4,h5,h6 {
	margin:0;
}

.container {
	max-width: 1000px;
	margin-right:auto;
	margin-left:auto;
	/* display:flex; */
	/* justify-content:center; */
	/* align-items:center; */
	min-height:100vh;
}

.table {
	width:100%;
	border:1px solid $color-form-highlight;
}

.table-header {
	/* display:flex; */
	width:100%;
	background:#000;
	padding:($half-spacing-unit * 1.5) 0;
}

.table-row {
	/* display:flex; */
	width:100%;
	padding:($half-spacing-unit * 1.5) 0;
	
	&:nth-of-type(odd) {
		background:$color-form-highlight;
	}
}

.table-row.denied {
    background: orange;
    text-decoration: line-through;
}
.table-row.error {
    background: red;
}
.table-row.older {
    background: orange;
    text-decoration: overline;
}
.table-row.done {
}
.table-row.nobuild {
    background: lightgrey;
}
.table-row.missing {
    background: lightgrey;
}
.table-row.need_build {
    background: lightgreen;
}
.table-row.need_build_free {
    background: lightgreen;
    text-decoration: overline;
}
.table-row.need_build_open {
    background: lightgreen;
    text-decoration: overline;
}
.table-row.need_build_closed {
    background: lightred;
    text-decoration: overline;
}
.table-row.need_build_canceled {
    background: red;
    text-decoration: overline;
}
.table-row.need_build_assigned {
    background: lightgreen;
    text-decoration: overline;
}
.table-row.need_build_failed {
    background: red;
}
.table-row.need_build_unknown {
    background: red;
}
.table-row.need_build_manual {
    background: red;
}
.table-row.need_push {
    background: lightgreen;
    text-decoration: underline;
}
.table-row.need_signing {
    background: yellow;
}
.table-row.extra {
    background: lightblue;
}

.table-data, .header__item {
	/* flex: 1 1 20%; */
	text-align: left;
}

.header__item {
	text-transform: uppercase;
}

.filter__link {
	color: white;
	text-decoration: none;
	position: relative;
	display: inline-block;
	padding-left: 24px;
	padding-right: 24px;
}
.filter__link::after {
		content: '';
		position: absolute;
		color: white;
		right: -18px;
		font-size: 12px;
		top: 50%;
		transform: translateY(-50%);
}
	
.filter__link.desc::after {
		content: '(desc)';
}

.filter__link.asc::after {
		content: '(asc)';
}
            </style>
        </head>
        <body>
        <a href="unsigned-packages.txt">unsigned nvra</a> <br>
"""

# filter__link--number for build ids?

html_table = """\
        <table class="table">
		<tr class="table-header">
			<td class="header__item"><a id="packages" class="filter__link" href="#">Packages</a></td>
			<td class="header__item"><a id="status" class="filter__link" href="#">Status</a></td>
			<td class="header__item"><a id="note" class="filter__link" href="#">Note</a></td>
		</tr>
"""

html_footer = """\
		</table>
          <script src='https://cdnjs.cloudflare.com/ajax/libs/jquery/3.2.1/jquery.min.js'></script>
          <script id="rendered-js">
var properties = [
	'packages',
	'status',
	'note',
];

$.each( properties, function( i, val ) {
	
	var orderClass = '';

	$("#" + val).click(function(e){
		e.preventDefault();
		$('.filter__link.filter__link--active').not(this).removeClass('filter__link--active');
  		$(this).toggleClass('filter__link--active');
   		$('.filter__link').removeClass('asc desc');

   		if(orderClass == 'desc' || orderClass == '') {
    			$(this).addClass('asc');
    			orderClass = 'asc';
       	} else {
       		$(this).addClass('desc');
       		orderClass = 'desc';
       	}

		var parent = $(this).closest('.header__item');
    		var index = $(".header__item").index(parent);
		var $table = $('.table');
		var rows = $table.find('.table-row').get();
		var isSelected = $(this).hasClass('filter__link--active');
		var isNumber = $(this).hasClass('filter__link--number');
			
		rows.sort(function(a, b){

			var x = $(a).find('.table-data').eq(index).text();
    			var y = $(b).find('.table-data').eq(index).text();
				
			if(isNumber == true) {
    					
				if(isSelected) {
					return x - y;
				} else {
					return y - x;
				}

			} else {
			
				if(isSelected) {		
					if(x < y) return -1;
					if(x > y) return 1;
					return 0;
				} else {
					if(x > y) return -1;
					if(x < y) return 1;
					return 0;
				}
			}
    		});

		$.each(rows, function(index,row) {
			$table.append(row);
		});

		return false;
	});

});
            </script>
        </body>
    </html>
"""

def html_row(fo, *args, **kwargs):
    lc = kwargs.get('lc')
    if lc is None:
        lc = ''
    else:
        lc = " " + str(lc)
    links = kwargs.get('links', {})

    fo.write("""\
    <tr class="table-row%s">
""" % (lc,))
    for arg in args:
        if arg in links:
            arg = '<a href="%s">%s</a>' % (links[arg], arg)
        fo.write("""\
		<td class="table-data">%s</td>
""" % (arg,))
    fo.write("""\
	</tr>
""")

# Key:
#  cpkg == compose package
#  bpkg == koji build tag package
#  tpkg == git tag package
def html_main(kapi, fo, cpkgs,cbpkgs, bpkgs,
              filter_pushed=False, filter_signed=False, prefix=None):

    def _html_row(status, **kwargs):
        note = bpkg._html_note
        note = note or cpkg._html_note
        note = note or ""

        # Kind of hacky, but eh...
        if kwargs['lc'] == "need_build":
            tid, state = _koji_pkg2task_state(kapi, cpkg)
            if False: pass
            elif state == 'NONE':
                pass # No build yet
            elif state == 'FREE':
                kwargs['lc'] = "need_build_free"
            elif state == 'OPEN':
                kwargs['lc'] = "need_build_open"
            elif state == 'CLOSED':
                kwargs['lc'] = "need_build_closed"
            elif state == 'CANCELED':
                kwargs['lc'] = "need_build_canceled"
            elif state == 'ASSIGNED':
                kwargs['lc'] = "need_build_assigned"
            elif state == 'FAILED':
                kwargs['lc'] = "need_build_failed"
            else:
                kwargs['lc'] = "need_build_unknown"

            if tid is not None:
                if 'links' not in kwargs:
                    kwargs['links'] = {}
                weburl = "https://koji.mbox.centos.org/koji/"
                weburl += "taskinfo?taskID=%d"
                weburl %= tid
                kwargs['links'][cpkg] = weburl

            if not note: # Auto notes based on auto filtering...
                if spkg._is_rebuild(cpkg):
                    note = "Rebuild"
                if spkg._is_branch_el8(cpkg):
                    note = "Branch"
                if spkg._is_module(cpkg):
                    note = "Module"
                if note:
                    if kwargs['lc'] == "need_build":
                        kwargs['lc'] = "need_build_manual"

        html_row(fo, cpkg, status, note, **kwargs)

    fo.write(html_header)

    if prefix:
        prefix(fo)

    pushed = {}
    for bpkg in bpkgs:
        pushed[bpkg.name] = bpkg

    tcoroot = tempfile.TemporaryDirectory(prefix="sync2html-", dir="/tmp")
    corootdir = tcoroot.name + '/'

    fo.write(html_table)
    stats = {'sign' : 0, 'done' : 0, 'push' : 0, 'build' : 0, 'denied' : 0,
             'missing' : 0, 'extra' : 0, 'git-old' : 0, 'tag-old' : 0,
             'error' : 0}
    for cpkg in sorted(cpkgs):
        denied = ml_pkgdeny.nvr(cpkg.name, cpkg.version, cpkg.release)

        if cpkg.name not in pushed:
            if denied:
                _html_row("denied", lc="denied")
                stats['denied'] += 1
                continue
            # html_row(fo, cpkg, "MISSING", lc="missing")
            stats['missing'] += 1
            bpkg = cpkg
        else:
            bpkg = pushed[cpkg.name]
            weburl = "https://koji.mbox.centos.org/koji/"
            weburl += "buildinfo?buildID=%d"
            weburl %= bpkg._koji_build_id
            links = {cpkg : weburl}
            if cpkg == bpkg:
                if not filter_signed and not bpkg.signed:
                    _html_row("built not signed", lc="need_signing",
                              links=links)
                    stats['sign'] += 1
                elif not filter_pushed:
                    _html_row("built and signed", lc="done", links=links)
                    stats['done'] += 1
                continue
            if cpkg < bpkg:
                _html_row("OLDER than build: " + str(bpkg), lc="older",
                          links=links)
                stats['tag-old'] += 1
                continue
            if cpkg > bpkg:
                if denied:
                    sbpkg = " " + str(bpkg)
                    _html_row("autobuild denied:"+ sbpkg, lc="denied")
                    stats['denied'] += 1
                    continue
                # html_row(fo, cpkg, "BUILD needed, latest build: " + str(bpkg), lc="need_build")
            else:
                _html_row("ERROR: " + str(bpkg), lc="error")
                stats['error'] += 1

        # cpkg > bpkg, or no bpkg
        codir = corootdir + bpkg.name
        tpkgs = _tags2pkgs(bpkg2git_tags(bpkg, codir))
        found = False
        for tpkg in reversed(sorted(tpkgs)):
            if tpkg.name != cpkg.name:
                continue
            found = True
            # This is the newest version in git...
            if cpkg < tpkg:
                _html_row("OLDER than git: " + str(tpkg), lc="older")
                stats['git-old'] += 1
                continue # See if the next oldest is ==
            if cpkg == tpkg:
                if cpkg == bpkg:
                    _html_row("No BUILD", lc="nobuild")
                else:
                    _html_row("BUILD needed, latest build: " + str(bpkg), lc="need_build")
                stats['build'] += 1
                break
            if cpkg > tpkg:
                _html_row("PUSH needed, latest git: " + str(tpkg), lc="need_push")
                stats['push'] += 1
                break

            _html_row("Error: bpkg: " + str(bpkg) + " tpkg: ", str(tpkg), lc="error")
            stats['error'] += 1
        if not found:
            _html_row("Missing from git", lc="missing")
            stats['push'] += 1

    if False:
        # Comparing a compose to a tag gives way too many extras...
        del pushed
        composed = {}
        for cpkg in cbpkgs:
            composed[cpkg.name] = cpkg
        for bpkg in sorted(bpkgs):
            if bpkg.name in composed:
                continue
            html_row(fo, bpkg, "extra", "", lc="extra")
            stats['extra'] += 1

    fo.write(html_footer)

    return stats

def _read_note(fname):
    if not os.path.exists(fname):
        return None
    return open(fname).read()

def read_note(basedir, pkg):
    for k in (pkg.nvra, pkg.nvr, pkg.nv, pkg.name):
        note = _read_note(basedir + '/' + k)
        if note is not None:
            return note

    return None

def read_notes(basedir, pkgs):
    for pkg in pkgs:
        pkg._html_note = read_note(basedir, pkg)

def main():
    parser = OptionParser()
    parser.add_option("", "--to-koji-host", dest="koji_host",
                      help="Host to connect to", default="https://koji.mbox.centos.org/kojihub")
    parser.add_option("", "--to-packages-tag", dest="packages_tag",
                      help="Specify package tag to sync2", default="dist-c8-stream")
    parser.add_option("", "--to-modules-tag", dest="modules_tag",
                      help="Specify module tag to sync2", default="dist-c8-stream-module")
    parser.add_option("", "--from-packages-compose", dest="packages_compose",
                      help="Specify package compose to sync", default="http://download.eng.bos.redhat.com/rhel-8/nightly/RHEL-8/latest-RHEL-8.4/")
    parser.add_option("", "--from-modules-compose", dest="modules_compose",
                      help="Specify module compose to sync", default="http://download.eng.bos.redhat.com/rhel-8/nightly/RHEL-8/latest-RHEL-8.4")
    parser.add_option("", "--notes", dest="notes",
                      help="Specify basedir to package notes", default="notes")


    (options, args) = parser.parse_args()

    tkapi = koji.ClientSession(options.koji_host)
    tkapi.ssl_login("/compose/.koji/mbox_admin.pem", None, "/compose/.koji/ca.crt")


    load_package_denylist()

    cpkgs, cbpkgs, cid, cstat = composed_url2pkgs(options.packages_compose)
    bpkgs = koji_tag2pkgs(tkapi, options.packages_tag)
    bpkgs = koji_pkgs2archsigs(tkapi, bpkgs)

    read_notes(options.notes, bpkgs)
    read_notes(options.notes, cpkgs)

    if not args: pass
    elif args[0] in ('packages', 'pkgs'):
        html_main(tkapi, sys.stdout, cpkgs, cbpkgs, bpkgs)
    elif args[0] in ('filtered-packages', 'filtered-pkgs', 'filt-pkgs'):
        html_main(tkapi, sys.stdout, cpkgs, cbpkgs, bpkgs, filter_pushed=True)
    elif args[0] in ('output-files',):
        print("Compose:", cid, cstat)

        tmhtml = '<h3> Generated:'
        tmhtml += time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())

        fo = open("all-packages.html", "w")
        prehtml = '<h2><a href="filt-packages.html">All</a> packages: ' +  cid
        prehtml += tmhtml
        pkghtml = '<p>RHEL Packages: %d (%d bin packages)'
        pkghtml %= (len(cpkgs), len(cbpkgs))
        prehtml += pkghtml
        sbpkgs = [x for x in bpkgs if x.arch == 'src']
        pkghtml = '<p>%s Packages: %d (%d bin packages)'
        pkghtml %= (options.packages_tag, len(sbpkgs), len(bpkgs))
        prehtml += pkghtml
        pre = lambda x: x.write(prehtml)
        stats = html_main(tkapi, fo, cpkgs, cbpkgs, bpkgs, filter_pushed=False, prefix=pre)

        fo = open("filt-packages.html", "w")
        prehtml = '<h2><a href="all-packages.html">Filtered</a> packages: ' +  cid
        prehtml += tmhtml
        for stat in sorted(stats):
            if stats[stat] == 0:
                continue
            pkghtml = '<p>%s Packages: %d'
            pkghtml %= (stat, stats[stat])
            prehtml += pkghtml

        pre = lambda x: x.write(prehtml)
        html_main(tkapi, fo, cpkgs, cbpkgs, bpkgs, filter_pushed=True, prefix=pre)
    else:
        print("Args: filtereed-packages | packages")

# Badly written but working python script
if __name__ == "__main__":
    main()
