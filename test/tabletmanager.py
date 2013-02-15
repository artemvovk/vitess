#!/usr/bin/python

import warnings
# Dropping a table inexplicably produces a warning despite
# the "IF EXISTS" clause. Squelch these warnings.
warnings.simplefilter("ignore")

import gzip
import os
import shutil
import socket
from subprocess import PIPE, call
import sys
import time

import MySQLdb

import utils
import tablet

devnull = open('/dev/null', 'w')
vttop = os.environ['VTTOP']
vtroot = os.environ['VTROOT']
vtdataroot = os.environ.get('VTDATAROOT', '/vt')
hostname = socket.gethostname()

tablet_62344 = tablet.Tablet(62344, 6700, 3700)
tablet_62044 = tablet.Tablet(62044, 6701, 3701)
tablet_41983 = tablet.Tablet(41983, 6702, 3702)
tablet_31981 = tablet.Tablet(31981, 6703, 3703)

def setup():
  utils.zk_setup()

  # start mysql instance external to the test
  setup_procs = [
      tablet_62344.start_mysql(),
      tablet_62044.start_mysql(),
      tablet_41983.start_mysql(),
      tablet_31981.start_mysql(),
      ]
  utils.wait_procs(setup_procs)

def teardown():
  if utils.options.skip_teardown:
    return

  teardown_procs = [
      tablet_62344.teardown_mysql(),
      tablet_62044.teardown_mysql(),
      tablet_41983.teardown_mysql(),
      tablet_31981.teardown_mysql(),
      ]
  utils.wait_procs(teardown_procs, raise_on_error=False)

  utils.zk_teardown()
  utils.kill_sub_processes()
  utils.remove_tmp_files()

  tablet_62344.remove_tree()
  tablet_62044.remove_tree()
  tablet_41983.remove_tree()
  tablet_31981.remove_tree()

  path = os.path.join(vtdataroot, 'snapshot')
  try:
    shutil.rmtree(path)
  except OSError as e:
    if utils.options.verbose:
      print >> sys.stderr, e, path

def _check_db_addr(db_addr, expected_addr):
  # Run in the background to capture output.
  proc = utils.run_bg(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING -zk.local-cell=test_nj Resolve ' + db_addr, stdout=PIPE)
  stdout = proc.communicate()[0].strip()
  if stdout != expected_addr:
    raise utils.TestError('wrong zk address', db_addr, stdout, expected_addr)


@utils.test_case
def run_test_sanity():
  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')
  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('RebuildKeyspaceGraph /zk/global/vt/keyspaces/test_keyspace')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  # if these statements don't run before the tablet it will wedge waiting for the
  # db to become accessible. this is more a bug than a feature.
  tablet_62344.populate('vt_test_keyspace', create_vt_select_test,
                        populate_vt_select_test)

  tablet_62344.start_vttablet()

  # make sure the query service is started right away
  result, _ = utils.run_vtctl('Query /zk/test_nj/vt/ns/test_keyspace "select * from vt_select_test"', trap_output=True)
  rows = result.splitlines()
  if len(rows) != 5:
    raise utils.TestError("expected 5 rows in vt_select_test", rows, result)

  # check Ping
  utils.run_vtctl('Ping ' + tablet_62344.zk_tablet_path)

  # Quickly check basic actions.
  utils.run_vtctl('SetReadOnly ' + tablet_62344.zk_tablet_path)
  utils.wait_db_read_only(62344)

  utils.run_vtctl('SetReadWrite ' + tablet_62344.zk_tablet_path)
  utils.check_db_read_write(62344)

  utils.run_vtctl('DemoteMaster ' + tablet_62344.zk_tablet_path)
  utils.wait_db_read_only(62344)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')
  utils.run_vtctl('ValidateKeyspace /zk/global/vt/keyspaces/test_keyspace')
  # not pinging tablets, as it enables replication checks, and they
  # break because we only have a single master, no slaves
  utils.run_vtctl('ValidateShard -ping-tablets=false /zk/global/vt/keyspaces/test_keyspace/shards/0')

  tablet_62344.kill_vttablet()

  tablet_62344.init_tablet('idle')
  utils.run_vtctl('ScrapTablet -force ' + tablet_62344.zk_tablet_path)


@utils.test_case
def run_test_scrap():
  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  tablet_62044.init_tablet('replica', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  utils.run_vtctl('ScrapTablet -force ' + tablet_62044.zk_tablet_path)
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')


create_vt_insert_test = '''create table vt_insert_test (
id bigint auto_increment,
msg varchar(64),
primary key (id)
) Engine=InnoDB'''

populate_vt_insert_test = [
    "insert into vt_insert_test (msg) values ('test %s')" % x
    for x in xrange(4)]

# the varbinary test uses 10 bytes long ids, stored in a 64 bytes column
# (using 10 bytes so it's longer than a uint64 8 bytes)
create_vt_insert_test_varbinary = '''create table vt_insert_test (
id varbinary(64),
msg varchar(64),
primary key (id)
) Engine=InnoDB'''

populate_vt_insert_test_varbinary = [
    "insert into vt_insert_test (id, msg) values (0x%02x000000000000000000, 'test %s')" % (x+1, x+1)
    for x in xrange(4)]

create_vt_select_test = '''create table vt_select_test (
id bigint auto_increment,
msg varchar(64),
primary key (id)
) Engine=InnoDB'''

populate_vt_select_test = [
    "insert into vt_select_test (msg) values ('test %s')" % x
    for x in xrange(4)]


def _run_test_mysqlctl_clone(server_mode):
  if server_mode:
    snapshot_cmd = "snapshotsourcestart -concurrency=8"
    restore_flags = "-dont-wait-for-slave-start"
  else:
    snapshot_cmd = "snapshot -concurrency=5"
    restore_flags = ""

  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create_vt_insert_test,
                        populate_vt_insert_test)

  tablet_62344.start_vttablet()

  err = tablet_62344.mysqlctl('-port 6700 -mysql-port 3700 %s vt_snapshot_test' % snapshot_cmd).wait()
  if err != 0:
    raise utils.TestError('mysqlctl %s failed' % snapshot_cmd)

  utils.pause("%s finished" % snapshot_cmd)

  call(["touch", "/tmp/vtSimulateFetchFailures"])
  err = tablet_62044.mysqlctl('-port 6701 -mysql-port 3701 restore -fetch-concurrency=2 -fetch-retry-count=4 %s %s/snapshot/vt_0000062344/snapshot_manifest.json' % (restore_flags, vtdataroot)).wait()
  if err != 0:
    raise utils.TestError('mysqlctl restore failed')

  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)

  if server_mode:
    err = tablet_62344.mysqlctl('-port 6700 -mysql-port 3700 snapshotsourceend -read-write vt_snapshot_test').wait()
    if err != 0:
      raise utils.TestError('mysqlctl snapshotsourceend failed')

    # see if server restarted properly
    tablet_62344.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)

  tablet_62344.kill_vttablet()

@utils.test_case
def run_test_mysqlctl_clone():
  _run_test_mysqlctl_clone(False)

@utils.test_case
def run_test_mysqlctl_clone_server():
  _run_test_mysqlctl_clone(True)

def _run_test_vtctl_snapshot_restore(server_mode):
  if server_mode:
    snapshot_flags = '-server-mode -concurrency=8'
    restore_flags = '-dont-wait-for-slave-start'
  else:
    snapshot_flags = '-concurrency=4'
    restore_flags = ''
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create_vt_insert_test,
                        populate_vt_insert_test)

  tablet_62044.create_db('vt_snapshot_test')

  tablet_62344.start_vttablet()

  # Need to force snapshot since this is a master db.
  out, err = utils.run_vtctl('Snapshot -force %s %s ' % (snapshot_flags, tablet_62344.zk_tablet_path), log_level='INFO', trap_output=True)
  results = {}
  for name in ['Manifest', 'ParentPath', 'SlaveStartRequired', 'ReadOnly', 'OriginalType']:
    sepPos = err.find(name + ": ")
    if sepPos != -1:
      results[name] = err[sepPos+len(name)+2:].splitlines()[0]
  if not "Manifest" in results:
    raise utils.TestError("Snapshot didn't echo Manifest file", err)
  if not "ParentPath" in results:
    raise utils.TestError("Snapshot didn't echo ParentPath", err)
  utils.pause("snapshot finished: " + results['Manifest'] + " " + results['ParentPath'])
  if server_mode:
    if not "SlaveStartRequired" in results:
      raise utils.TestError("Snapshot didn't echo SlaveStartRequired", err)
    if not "ReadOnly" in results:
      raise utils.TestError("Snapshot didn't echo ReadOnly", err)
    if not "OriginalType" in results:
      raise utils.TestError("Snapshot didn't echo OriginalType", err)
    if (results['SlaveStartRequired'] != 'false' or
        results['ReadOnly'] != 'true' or
        results['OriginalType'] != 'master'):
      raise utils.TestError("Bad values returned by Snapshot", err)
  tablet_62044.init_tablet('idle', start=True)

  # do not specify a MANIFEST, see if 'default' works
  call(["touch", "/tmp/vtSimulateFetchFailures"])
  utils.run_vtctl('Restore -fetch-concurrency=2 -fetch-retry-count=4 %s %s default %s %s' %
                  (restore_flags, tablet_62344.zk_tablet_path,
                   tablet_62044.zk_tablet_path, results['ParentPath']), auto_log=True)
  utils.pause("restore finished")

  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  # in server_mode, get the server out of it and check it
  if server_mode:
    utils.run_vtctl('SnapshotSourceEnd %s %s' % (tablet_62344.zk_tablet_path, results['OriginalType']), auto_log=True)
    tablet_62344.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)
    utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()

@utils.test_case
def run_test_vtctl_snapshot_restore():
  _run_test_vtctl_snapshot_restore(server_mode=False)

@utils.test_case
def run_test_vtctl_snapshot_restore_server():
  _run_test_vtctl_snapshot_restore(server_mode=True)

def _run_test_vtctl_clone(server_mode):
  if server_mode:
    clone_flags = '-server-mode'
  else:
    clone_flags = ''
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create_vt_insert_test,
                        populate_vt_insert_test)
  tablet_62344.start_vttablet()

  tablet_62044.create_db('vt_snapshot_test')
  tablet_62044.init_tablet('idle', start=True)

  # small test to make sure the directory validation works
  snapshot_dir = os.path.join(vtdataroot, 'snapshot')
  utils.run("rm -rf %s" % snapshot_dir)
  utils.run("mkdir -p %s" % snapshot_dir)
  utils.run("chmod -w %s" % snapshot_dir)
  out, err = utils.run(vtroot+'/bin/vtctl -logfile=/dev/null Clone -force %s %s %s' %
                       (clone_flags, tablet_62344.zk_tablet_path,
                        tablet_62044.zk_tablet_path),
                       trap_output=True, raise_on_error=False)
  if err.find("Cannot validate snapshot directory") == -1:
    raise utils.TestError("expected validation error", err)
  utils.run("chmod +w %s" % snapshot_dir)

  # the snapshot failed, which means Clone left the destination tablet
  # in scrap mode, so we need to re-init it
  tablet_62044.init_tablet('idle')

  call(["touch", "/tmp/vtSimulateFetchFailures"])
  utils.run_vtctl('Clone -force %s %s %s' %
                  (clone_flags, tablet_62344.zk_tablet_path,
                   tablet_62044.zk_tablet_path))

  utils.pause("look at logs!")
  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)
  tablet_62344.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()

@utils.test_case
def run_test_vtctl_clone():
  _run_test_vtctl_clone(server_mode=False)

@utils.test_case
def run_test_vtctl_clone_server():
  _run_test_vtctl_clone(server_mode=True)

@utils.test_case
def test_multisnapshot_and_restore():
  create = '''create table vt_insert_test (
id bigint auto_increment,
msg varchar(64),
primary key (id)
) Engine=InnoDB'''
  insert_template = "insert into vt_insert_test (id, msg) values (%s, 'test %s')"
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # Start three tablets for three different shards. At this point the
  # sharding schema is not really important, as long as it is
  # consistent.
  new_spec = '-0000000000000028-'
  old_tablets = [tablet_62044, tablet_41983, tablet_31981]
  for i, tablet in enumerate(old_tablets):
    tablet.init_tablet('master', 'test_keyspace', str(i))
    utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/%s' % i)
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  for i, tablet in enumerate(old_tablets):
    tablet.populate("vt_test_keyspace", create, [insert_template % (10*j + i, 10*j + i)
                                                 for j in range(1, 8)])
    tablet.start_vttablet()
    print utils.run_vtctl('MultiSnapshot --force  --spec=%s %s id' % (new_spec, tablet.zk_tablet_path), trap_output=True)


  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace_new')
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62344.init_tablet('master', 'test_keyspace_new', "0", dbname="vt_test_keyspace")
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace_new/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')
  tablet_62344.start_vttablet()

  tablet_urls = ' '.join("http://localhost:%s" % tablet.port for tablet in old_tablets)

  # 0x28 = 40
  tablet_62344.mysqlctl("multirestore --end=0000000000000028 vt_test_keyspace %s" % tablet_urls)
  #utils.run_vtctl('MultiRestore --end=0000000000000028 %s id %s' % (tablet_urls, tablet.zk_tablet_path), trap_output=True, raise_on_error=True)
  time.sleep(1)
  rows = tablet_62344.mquery('vt_test_keyspace', 'select id from vt_insert_test')
  if len(rows) == 0:
    raise utils.TestError("There are no rows in the restored database.")
  for row in rows:
    if row[0] > 32:
      raise utils.TestError("Bad row: %s" % row)
  for tablet in tablet_62044, tablet_41983, tablet_31981, tablet_62344:
    tablet.kill_vttablet()


@utils.test_case
def test_multisnapshot_mysqlctl():
  populate = sum([[
    "insert into vt_insert_test_%s (msg) values ('test %s')" % (i, x)
    for x in xrange(4)] for i in range(6)], [])
  create = ['''create table vt_insert_test_%s (
id bigint auto_increment,
msg varchar(64),
primary key (id)
) Engine=InnoDB''' % i for i in range(6)]

  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_test_keyspace', create,
                        populate)

  tablet_62344.start_vttablet()
  err = tablet_62344.mysqlctl('-port 6700 -mysql-port 3700 multisnapshot --tables=vt_insert_test_1,vt_insert_test_2,vt_insert_test_3 --spec=-0000000000000003- vt_test_keyspace id').wait()
  if err != 0:
    raise utils.TestError('mysqlctl multisnapshot failed')
  if os.path.exists(os.path.join(vtdataroot, 'snapshot/vt_0000062344/data/vt_test_keyspace-,0000000000000003/vt_insert_test_4.csv.gz')):
    raise utils.TestError("Table vt_insert_test_4 wasn't supposed to be dumped.")
  for kr in 'vt_test_keyspace-,0000000000000003', 'vt_test_keyspace-0000000000000003,':
    path = os.path.join(vtdataroot, 'snapshot/vt_0000062344/data/', kr, 'vt_insert_test_1.csv.gz')
    with gzip.open(path) as f:
      if len(f.readlines()) != 2:
        raise utils.TestError("Data looks wrong in %s" % path)


@utils.test_case
def test_multisnapshot_vtctl():
  populate = sum([[
    "insert into vt_insert_test_%s (msg) values ('test %s')" % (i, x)
    for x in xrange(4)] for i in range(6)], [])
  create = ['''create table vt_insert_test_%s (
id bigint auto_increment,
msg varchar(64),
primary key (id)
) Engine=InnoDB''' % i for i in range(6)]

  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_test_keyspace', create,
                        populate)

  tablet_62344.start_vttablet()

  utils.run_vtctl('MultiSnapshot --force --tables=vt_insert_test_1,vt_insert_test_2,vt_insert_test_3 --spec=-0000000000000003- %s id' % tablet_62344.zk_tablet_path)

  # if err != 0:
  #   raise utils.TestError('mysqlctl multisnapshot failed')
  if os.path.exists(os.path.join(vtdataroot, 'snapshot/vt_0000062344/data/vt_test_keyspace-,0000000000000003/vt_insert_test_4.csv.gz')):
    raise utils.TestError("Table vt_insert_test_4 wasn't supposed to be dumped.")
  for kr in 'vt_test_keyspace-,0000000000000003', 'vt_test_keyspace-0000000000000003,':
    path = os.path.join(vtdataroot, 'snapshot/vt_0000062344/data/', kr, 'vt_insert_test_1.csv.gz')
    with gzip.open(path) as f:
      if len(f.readlines()) != 2:
        raise utils.TestError("Data looks wrong in %s" % path)


@utils.test_case
def run_test_mysqlctl_split():
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_test_keyspace', create_vt_insert_test,
                        populate_vt_insert_test)

  tablet_62344.start_vttablet()

  err = tablet_62344.mysqlctl('-port 6700 -mysql-port 3700 partialsnapshot -end=0000000000000003 vt_test_keyspace id').wait()
  if err != 0:
    raise utils.TestError('mysqlctl partialsnapshot failed')


  utils.pause("partialsnapshot finished")

  tablet_62044.mquery('', 'stop slave')
  tablet_62044.create_db('vt_test_keyspace')
  call(["touch", "/tmp/vtSimulateFetchFailures"])
  err = tablet_62044.mysqlctl('-port 6701 -mysql-port 3701 partialrestore %s/snapshot/vt_0000062344/data/vt_test_keyspace-,0000000000000003/partial_snapshot_manifest.json' % vtdataroot).wait()
  if err != 0:
    raise utils.TestError('mysqlctl partialrestore failed')

  tablet_62044.assert_table_count('vt_test_keyspace', 'vt_insert_test', 2)

  # change/add two values on the master, one in range, one out of range, make
  # sure the right one propagate and not the other
  utils.run_vtctl('SetReadWrite ' + tablet_62344.zk_tablet_path)
  tablet_62344.mquery('vt_test_keyspace', "insert into vt_insert_test (id, msg) values (5, 'test should not propagate')", write=True)
  tablet_62344.mquery('vt_test_keyspace', "update vt_insert_test set msg='test should propagate' where id=2", write=True)

  utils.pause("look at db now!")

  # wait until value that should have been changed is here
  timeout = 10
  while timeout > 0:
    result = tablet_62044.mquery('vt_test_keyspace', 'select msg from vt_insert_test where id=2')
    if result[0][0] == "test should propagate":
      break
    timeout -= 1
    time.sleep(1)
  if timeout == 0:
    raise utils.TestError("expected propagation to happen", result)

  # test value that should not propagate
  # this part is disabled now, as the replication pruning is only enabled
  # for row-based replication, but the mysql server is statement based.
  # will re-enable once we get statement-based pruning patch into mysql.
#  tablet_62044.assert_table_count('vt_test_keyspace', 'vt_insert_test', 0, 'where id=5')

  tablet_62344.kill_vttablet()

def _run_test_vtctl_partial_clone(create, populate,
                                  start, end):
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create, populate)

  tablet_62344.start_vttablet()

  tablet_62044.init_tablet('idle', start=True)

  # FIXME(alainjobart): not sure where the right place for this is,
  # but it doesn't seem it should right here. It should be either in
  # InitTablet (running an action on the vttablet), or in PartialClone
  # (instead of doing a 'USE dbname' it could do a 'CREATE DATABASE
  # dbname').
  tablet_62044.mquery('', 'stop slave')
  tablet_62044.create_db('vt_snapshot_test')
  call(["touch", "/tmp/vtSimulateFetchFailures"])
  utils.run_vtctl('PartialClone -force %s %s id %s %s' %
                  (tablet_62344.zk_tablet_path, tablet_62044.zk_tablet_path,
                   start, end))

  utils.pause("after PartialClone")

  # grab the new tablet definition from zk, make sure the start and
  # end keys are set properly
  out, err = utils.run(vtroot+'/bin/zk cat ' + tablet_62044.zk_tablet_path,
                       trap_output=True)
  if (out.find('"Start": "%s"' % start) == -1 or \
        out.find('"End": "%s"' % end) == -1):
    print "Tablet output:"
    print "out"
    raise utils.TestError('wrong Start or End')

  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 2)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()

@utils.test_case
def run_test_vtctl_partial_clone():
  _run_test_vtctl_partial_clone(create_vt_insert_test,
                                populate_vt_insert_test,
                                '0000000000000000',
                                '0000000000000003')

@utils.test_case
def run_test_vtctl_partial_clone_varbinary():
  _run_test_vtctl_partial_clone(create_vt_insert_test_varbinary,
                                populate_vt_insert_test_varbinary,
                                '00000000000000000000',
                                '03000000000000000000')

@utils.test_case
def run_test_restart_during_action():
  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62344.start_vttablet()

  utils.run_vtctl('Ping ' + tablet_62344.zk_tablet_path)

  # schedule long action
  utils.run_vtctl('-no-wait Sleep %s 15s' % tablet_62344.zk_tablet_path, stdout=devnull)
  # ping blocks until the sleep finishes unless we have a schedule race
  action_path, _ = utils.run_vtctl('-no-wait Ping ' + tablet_62344.zk_tablet_path, trap_output=True)

  # kill agent leaving vtaction running
  tablet_62344.kill_vttablet()

  # restart agent
  tablet_62344.start_vttablet()

  # we expect this action with a short wait time to fail. this isn't the best
  # and has some potential for flakiness.
  utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING -wait-time 2s WaitForAction ' + action_path)

  # wait until the background sleep action is done, otherwise there will be
  # a leftover vtaction whose result may overwrite running actions
  # NOTE(alainjobart): Yes, I've seen it happen, it's a pain to debug:
  # the zombie Sleep clobbers the Clone command in the following tests
  utils.run(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING -wait-time 20s WaitForAction ' + action_path)

  # extra small test: we ran for a while, get the states we were in,
  # make sure they're accounted for properly
  # first the query engine States
  v = utils.get_vars(tablet_62344.port)
  if v['Voltron']['States']['DurationOPEN'] < 10e9:
    raise utils.TestError('not enough time in Open state', v['Voltron']['States']['DurationOPEN'])
  # then the Zookeeper connections
  if v['ZkMetaConn']['test_nj']['Current'] != 'Connected':
    raise utils.TestError('invalid zk test_nj state: ', v['ZkMetaConn']['test_nj']['Current'])
  if v['ZkMetaConn']['global']['Current'] != 'Connected':
    raise utils.TestError('invalid zk global state: ', v['ZkMetaConn']['global']['Current'])
  if v['ZkMetaConn']['test_nj']['DurationConnected'] < 10e9:
    raise utils.TestError('not enough time in Connected state', v['ZkMetaConn']['test_nj']['DurationConnected'])

  tablet_62344.kill_vttablet()

@utils.test_case
def run_test_reparent_down_master():
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', '0', start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', '0', start=True)
  tablet_41983.init_tablet('replica', 'test_keyspace', '0', start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', '0', start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/0 ' + tablet_62344.zk_tablet_path, auto_log=True)
  utils.zk_check()

  # Make the master agent unavailable.
  tablet_62344.kill_vttablet()


  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.0.master:_vtocc', expected_addr)

  # Perform a reparent operation - the Validate part will try to ping
  # the master and fail somewhat quickly
  stdout, stderr = utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=INFO -wait-time 5s ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/0 ' + tablet_62044.zk_tablet_path)
  utils.debug("Failed ReparentShard output:\n" + stderr)
  if stderr.find('dial failed') == -1 or stderr.find('ValidateShard verification failed') == -1:
    raise utils.TestError("didn't find the right error strings in failed ReparentShard: " + stderr)

  # Should timeout and fail
  stdout, stderr = utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=INFO -wait-time 5s ScrapTablet ' + tablet_62344.zk_tablet_path)
  utils.debug("Failed ScrapTablet output:\n" + stderr)
  if stderr.find('deadline exceeded') == -1:
    raise utils.TestError("didn't find the right error strings in failed ScrapTablet: " + stderr)

  # Force the scrap action in zk even though tablet is not accessible.
  utils.run_vtctl('ScrapTablet -force -skip-rebuild ' + tablet_62344.zk_tablet_path, auto_log=True)

  utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING ChangeSlaveType -force %s idle' %
                 tablet_62344.zk_tablet_path)

  # Remove pending locks (make this the force option to ReparentShard?)
  utils.run_vtctl('PurgeActions /zk/global/vt/keyspaces/test_keyspace/shards/0/action')

  # Scrapping a tablet with -skip-rebuild shouldn't take it out of the
  # serving graph.
  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.0.master:_vtocc', expected_addr)

  # Re-run reparent operation, this shoud now proceed unimpeded.
  utils.run_vtctl('-wait-time 3m ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/0 ' + tablet_62044.zk_tablet_path, auto_log=True)

  utils.zk_check()
  expected_addr = hostname + ':6701'
  _check_db_addr('test_keyspace.0.master:_vtocc', expected_addr)

  utils.run_vtctl('ChangeSlaveType -force %s idle' % tablet_62344.zk_tablet_path)

  idle_tablets, _ = utils.run_vtctl('ListIdle /zk/test_nj/vt', trap_output=True)
  if '0000062344' not in idle_tablets:
    raise utils.TestError('idle tablet not found', idle_tablets)

  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()
  tablet_31981.kill_vttablet()


@utils.test_case
def run_test_reparent_graceful_range_based():
  shard_id = '0000000000000000-FFFFFFFFFFFFFFFF'
  _run_test_reparent_graceful(shard_id)

@utils.test_case
def run_test_reparent_graceful():
  shard_id = '0'
  _run_test_reparent_graceful(shard_id)

def _run_test_reparent_graceful(shard_id):
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', shard_id, start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_41983.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', shard_id, start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/' + shard_id)
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.pause("force ReparentShard?")
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62344.zk_tablet_path))
  utils.zk_check(ping_tablets=True)

  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  # Convert two replica to spare. That should leave only one node serving traffic,
  # but still needs to appear in the replication graph.
  utils.run_vtctl('ChangeSlaveType ' + tablet_41983.zk_tablet_path + ' spare')
  utils.run_vtctl('ChangeSlaveType ' + tablet_31981.zk_tablet_path + ' spare')
  utils.zk_check()
  expected_addr = hostname + ':6701'
  _check_db_addr('test_keyspace.%s.replica:_vtocc' % shard_id, expected_addr)

  # Perform a graceful reparent operation.
  utils.pause("graceful ReparentShard?")
  utils.run_vtctl('ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62044.zk_tablet_path))
  utils.zk_check()

  expected_addr = hostname + ':6701'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()
  tablet_31981.kill_vttablet()

  # Test address correction.
  tablet_62044.start_vttablet(port=6773)
  # Wait a moment for address to reregister.
  time.sleep(1.0)

  expected_addr = hostname + ':6773'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  tablet_62044.kill_vttablet()


# This is a manual test to check error formatting.
@utils.test_case
def run_test_reparent_slave_offline(shard_id='0'):
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', shard_id, start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_41983.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', shard_id, start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/' + shard_id)
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62344.zk_tablet_path))
  utils.zk_check(ping_tablets=True)

  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  # Kill one tablet so we seem offline
  tablet_31981.kill_vttablet()

  # Perform a graceful reparent operation.
  utils.run_vtctl('ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62044.zk_tablet_path))

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()


# See if a lag slave can be safely reparent.
@utils.test_case
def run_test_reparent_lag_slave(shard_id='0'):
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', shard_id, start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_41983.init_tablet('lag', 'test_keyspace', shard_id, start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/' + shard_id)
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62344.zk_tablet_path))
  utils.zk_check(ping_tablets=True)

  tablet_62344.create_db('vt_test_keyspace')
  tablet_62344.mquery('vt_test_keyspace', create_vt_insert_test)

  tablet_41983.mquery('', 'stop slave')
  for q in populate_vt_insert_test:
    tablet_62344.mquery('vt_test_keyspace', q, write=True)

  # Perform a graceful reparent operation.
  utils.run_vtctl('ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62044.zk_tablet_path))

  tablet_41983.mquery('', 'start slave')
  time.sleep(1)

  utils.pause("check orphan")

  utils.run_vtctl('ReparentTablet %s' % tablet_41983.zk_tablet_path)

  result = tablet_41983.mquery('vt_test_keyspace', 'select msg from vt_insert_test where id=1')
  if len(result) != 1:
    raise utils.TestError('expected 1 row from vt_insert_test', result)

  utils.pause("check lag reparent")

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()
  tablet_31981.kill_vttablet()



@utils.test_case
def run_test_vttablet_authenticated():
  utils.zk_wipe()
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')
  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_test_keyspace', create_vt_select_test,
                        populate_vt_select_test)
  agent = tablet_62344.start_vttablet(auth=True)
  utils.run_vtctl('SetReadWrite ' + tablet_62344.zk_tablet_path)

  err, out = tablet_62344.vquery('select * from vt_select_test', dbname='vt_test_keyspace', user='ala', password=r'ma kota')
  if 'Row count: ' not in out:
    raise utils.TestError("query didn't go through: %s, %s" % (err, out))

  utils.kill_sub_process(agent)
  # TODO(szopa): Test that non-authenticated queries do not pass
  # through (when we get to that point).

def _check_string_in_hook_result(text, expected):
  if isinstance(expected, basestring):
    expected = [expected]
  for exp in expected:
    if text.find(exp) != -1:
      return
  print "ExecuteHook output:"
  print text
  raise utils.TestError("ExecuteHook returned unexpected result, no string: '" + "', '".join(expected) + "'")

def _run_hook(params, expectedStrings):
  out, err = utils.run(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=INFO ExecuteHook %s %s' % (tablet_62344.zk_tablet_path, params), trap_output=True, raise_on_error=False)
  for expected in expectedStrings:
    _check_string_in_hook_result(err, expected)

@utils.test_case
def run_test_hook():
  utils.zk_wipe()
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as it is serving
  tablet_62344.create_db('vt_test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0', start=True)

  # test a regular program works
  _run_hook("test.sh flag1 param1=hello", [
      '"ExitStatus": 0',
      ['"Stdout": "PARAM: --flag1\\nPARAM: --param1=hello\\n"',
       '"Stdout": "PARAM: --param1=hello\\nPARAM: --flag1\\n"',
       ],
      '"Stderr": ""',
      ])

  # test stderr output
  _run_hook("test.sh to-stderr", [
      '"ExitStatus": 0',
      '"Stdout": "PARAM: --to-stderr\\n"',
      '"Stderr": "ERR: --to-stderr\\n"',
      ])

  # test commands that fail
  _run_hook("test.sh exit-error", [
      '"ExitStatus": 1',
      '"Stdout": "PARAM: --exit-error\\n"',
      '"Stderr": "ERROR: exit status 1\\n"',
      ])

  # test hook that is not present
  _run_hook("not_here.sh", [
      '"ExitStatus": -1',
      '"Stdout": "Skipping missing hook: /', # cannot go further, local path
      '"Stderr": ""',
      ])

  # test hook with invalid name
  _run_hook("/bin/ls", [
      "FATAL: action failed: ExecuteHook hook name cannot have a '/' in it",
      ])

  tablet_62344.kill_vttablet()

@utils.test_case
def run_test_sigterm():
  utils.zk_wipe()
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as it is serving
  tablet_62344.create_db('vt_test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0', start=True)

  # start a 'vtctl Sleep' command in the background
  sp = utils.run_bg(vtroot+'/bin/vtctl -logfile=/dev/null Sleep %s 60s' %
                    tablet_62344.zk_tablet_path,
                    stdout=PIPE, stderr=PIPE)

  # wait for it to start, and let's kill it
  time.sleep(2.0)
  utils.run(['pkill', 'vtaction'])
  out, err = sp.communicate()

  # check the vtctl command got the right remote error back
  if err.find("vtaction interrupted by signal") == -1:
    raise utils.TestError("cannot find expected output in error:", err)
  utils.debug("vtaction was interrupted correctly:\n" + err)

  tablet_62344.kill_vttablet()

def run_all():
  run_test_sanity()
  run_test_sanity() # run twice to check behavior with existing znode data
  run_test_scrap()
  run_test_restart_during_action()

  # Subsumed by vtctl_clone* tests.
  # run_test_mysqlctl_clone()
  # run_test_mysqlctl_clone_server()
  # run_test_vtctl_snapshot_restore()
  # run_test_vtctl_snapshot_restore_server()
  run_test_vtctl_clone()
  run_test_vtctl_clone_server()

  # This test does not pass as it requires an experimental mysql patch.
  #run_test_vtctl_partial_clone()
  #run_test_vtctl_partial_clone_varbinary()

  run_test_reparent_graceful()
  run_test_reparent_graceful_range_based()
  run_test_reparent_down_master()
  run_test_vttablet_authenticated()
  run_test_reparent_lag_slave()
  run_test_hook()
  run_test_sigterm()

def main():
  args = utils.get_args()

  try:
    if args[0] != 'teardown':
      setup()
      if args[0] != 'setup':
        for arg in args:
          globals()[arg]()
          print "GREAT SUCCESS"
  except KeyboardInterrupt:
    pass
  except utils.Break:
    utils.options.skip_teardown = True
  finally:
    teardown()


if __name__ == '__main__':
  main()
