from warnings import filterwarnings
filterwarnings("ignore", message="Not using MPI as mpi4py not found")
filterwarnings("ignore", message="Can't drop database.*")

import os
import shutil
from glob import glob, glob1
import configparser
from collections import defaultdict
from pprint import pprint

import click
from cogent3.util import parallel

from ensembldb.download import read_config, reduce_dirnames, _cfg
from . import HostAccount
from .host import DbConnection
from .util import exec_command, open_, abspath, ENSEMBLDBRC
from .download import download_dbs

__author__ = "Gavin Huttley"
__copyright__ = "Copyright 2016-, The EnsemblDb Project"
__credits__ = ["Gavin Huttley"]
__license__ = "BSD"
__version__ = "3.0a1"
__maintainer__ = "Gavin Huttley"
__email__ = "Gavin.Huttley@anu.edu.au"
__status__ = "alpha"


def listpaths(dirname, glob_pattern):
    """return path to all files matching glob_pattern"""
    fns = glob1(dirname, glob_pattern)
    if not fns:
        return None
    fns = [os.path.join(dirname, fn) for fn in fns]
    return fns

def InstallTable(mysqlcfg, account, dbname, mysqlimport="mysqlimport", verbose=False, debug=False):
    """returns a function that requires path to the db table
    
    Parameters
    ----------
    mysqlcfg : path
      path to the mysql cfg file containing a mysqlimport section
    account : HostAccount
    dbname : str
      name of the database
    mysqlimport : str
      path to mysqlimport
    """
    info = read_mysql_config(mysqlcfg, "mysqlimport", verbose=verbose)
    command = info["command"] or r"mysqlimport --fields_escaped_by=\\"
    if None in [info["user"], info["passwd"]]:
        acct = r" -u %(user)s -p%(passwd)s "
    else:
        acct = ""
    
    host = "" if info["host"] is None else r" -h %(host)s "
    
    cmnd_template = command + host + acct + " %(dbname)s -L %(tablename)s"
    kwargs = dict(host=info["host"] or account.host,
                  user=info["user"] or account.user,
                  passwd=info["passwd"] or account.passwd,
                  dbname=dbname)
    
    def install_table(tablename):
        """installs a single table"""
        if tablename.endswith(".gz"):
            # gunzip
            r = exec_command("gunzip %s" % tablename)
        tablename = tablename.replace(".gz", "")
        # then install
        kwargs["tablename"] = tablename
        if debug:
            pprint(kwargs)
        
        cmnd = cmnd_template % kwargs
        if debug:
            print(cmnd)
        
        if verbose:
            print("\tinstalling %s" % tablename)
        
        exec_args = {} if not debug else dict(stderr=None, stdout=None)
        r = exec_command(cmnd, **exec_args)
        return r
    
    return install_table

def get_db_checkpoint_path(local_path, dbname):
    """returns path to db checkpoint file"""
    checkpoint_file = os.path.join(local_path, dbname, "ENSEMBLDB_DONE")
    return checkpoint_file

def is_installed(local_path, dbname):
    """returns True if checkpoint file exists for dbname"""
    chk = get_db_checkpoint_path(local_path, dbname)
    return os.path.exists(chk)

def install_one_db(mysqlcfg, cursor, account, dbname, local_path, numprocs, force_overwrite=False, verbose=False, debug=False):
    """installs a single ensembl database"""
    # first create the database in mysql
    # find the .sql file, load all contents into memory
    # then execute using mysql cursor?
    # ensembl instructions suggest the following
    # $ mysql -u uname dname < dbname.sql
    dbpath = os.path.join(local_path, dbname)
    if is_installed(local_path, dbname) and not force_overwrite:
        print("ALREADY INSTALLED: %s, skipping" % dbname)
        return True
    
    sqlfile = listpaths(dbpath, "*.sql*")
    if not sqlfile:
        raise RuntimeError("sql file not present in %s" % dbpath)
    
    sqlfile = sqlfile[0]
    with open_(sqlfile, mode='rt') as infile:
        sql = infile.readlines()
    sql = "\n".join(sql)
    # select the database
    if verbose or debug:
        print("\tcreating table definitions for %s" % dbname)
    r = cursor.execute("USE %s" % dbname)
    # make sure tables don't exist
    r = cursor.execute("SHOW TABLES")
    result = cursor.fetchall()
    for table in result:
        print(table)
        r = cursor.execute("DROP TABLE IF EXISTS %s" % table)
    
    # create the table definitions
    num_tables = sql.count("CREATE")
    r = cursor.execute(sql)
    
    # the following step seems necessary for mysql to actually create all the table
    # definitions... wtf?
    r = cursor.execute("SHOW TABLES")
    if r != num_tables:
        pprint(cursor.fetchall())
        raise RuntimeError("number of created tables doesn't match number in sql")
    
    if debug:
        print(r)
        print()
        display_dbs_tables(cursor, dbname)
    
    tablenames = listpaths(dbpath, "*.txt*")
    tablenames_gzipped = listpaths(dbpath, "*.txt.gz")
    # if uncompressed table exists, we'll remove it
    if tablenames and tablenames_gzipped:
        for name in tablenames_gzipped:
            name = name[:-3]
            if name in tablenames:
                if verbose:
                    print("\tWARN: Deleting %s, using compressed version" % name)
                tablenames.remove(name)
                os.remove(name)
    
    if debug:
        pprint(tablenames)
    
    install_table = InstallTable(mysqlcfg, account, dbname, verbose=verbose, debug=debug)
    
    if numprocs > 1:
        procs = parallel.MultiprocessingParallelContext(numprocs)
    else:
        procs = parallel.NonParallelContext()
    
    # we do the table install in parallel
    for r in procs.imap(install_table, tablenames):
        pass
    
    # existence of this file signals completion of the install without failure
    checkpoint_file = get_db_checkpoint_path(local_path, dbname)    
    with open(checkpoint_file, "w") as checked:
        pass
    

## can we get away with the ~/.my.cnf for the sql cursor?
def read_mysql_config(config_path, section, verbose=False):
    """returns a dict with mysql config options
    
    Parameters
    ----------
    config_path : str
      path to mysql config file
    section : str
      section in config file to query
    """
    opts = defaultdict(lambda: None)
    parser = configparser.ConfigParser(opts)
    parser.read_file(config_path)
    if not parser.has_section(section):
        return opts
    
    for k in ["host", "user", "passwd", "command"]:
        opts[k] = parser.get(section, k) or opts[k]
    
    return opts

def _drop_db(cursor, dbname):
    """drops the database"""
    sql = "DROP DATABASE IF EXISTS %s" % dbname
    cursor.execute(sql)

def display_dbs(cursor, release):
    """shows what databases for the nominated release exist at the server"""
    sql = "SHOW DATABASES"
    r = cursor.execute(sql)
    result = cursor.fetchall()
    for r in result:
        if isinstance(r, tuple):
            r = r[0]
        
        if release in r:
            pprint(r)
    
def display_dbs_tables(cursor, dbname):
    """shows what databases for the nominated release exist at the server"""
    #r = cursor.execute("USE %s" % dbname)
    sql = "SHOW TABLES"
    r = cursor.execute(sql)
    result = cursor.fetchall()
    for r in result:
        if isinstance(r, tuple):
            r = r[0]
        
        pprint(r)

# default mysql config    
_mycfg = os.path.join(ENSEMBLDBRC, 'mysql.cfg')

# defining some of the options
_cfgpath = click.option('-c', '--configpath', default=_cfg, type=click.File(),
              help="path to config file specifying databases, only species or compara at present")
_mysqlcfg = click.option('-m', '--mysqlcfg', default=_mycfg, type=click.File(),
              help="path to mysql config file specifying host, user, installing data for writing")
_verbose = click.option('-v', '--verbose', is_flag=True,
              help="causes stdout/stderr from rsync download to be written to screen")
_numprocs = click.option('-n', '--numprocs', type=int, default=1,
              help="number of processes to use for download")
_force = click.option('-f', '--force_overwrite', is_flag=True,
              help="drop existing database if it exists prior to installing")
_debug = click.option('-d', '--debug', is_flag=True,
              help="maximum verbosity")
_dbrc_out = click.option('-o', '--outpath', type=click.Path(),
                         help="path to directory to export all rc contents")

@click.group()
def main():
    """admin tools for an Ensembl MySQL installation"""
    pass

@main.command()
@_cfgpath
@_numprocs
@_verbose
@_debug
def download(configpath, numprocs, verbose, debug):
    """download databases from Ensembl using rsync, can be done in parallel"""
    download_dbs(configpath, numprocs, verbose, debug)

@main.command()
@_cfgpath
@_mysqlcfg
@_numprocs
@_force
@_verbose
@_debug
def install(configpath, mysqlcfg, numprocs, force_overwrite, verbose, debug):
    """install ensembl databases into a MySQL server"""
    mysql_info = read_mysql_config(mysqlcfg, "mysql")
    account = HostAccount(mysql_info["host"], mysql_info["user"],
                          mysql_info["passwd"])
    server = DbConnection(account, db_name='PARENT', pool_recycle=36000)
    
    release, local_path, species_dbs = read_config(configpath)
    content = os.listdir(local_path)
    dbnames = reduce_dirnames(content, species_dbs)
    for dbname in dbnames:
        server.ping(reconnect=True) # reconnect if server not alive
        cursor = server.cursor()
        if force_overwrite or not is_installed(local_path, dbname.name):
            _drop_db(cursor, dbname.name)
        
        if verbose:
            print("Creating database %s" % dbname.name)
        
        # now create dbname
        sql = "CREATE DATABASE IF NOT EXISTS %s" % dbname
        r = cursor.execute(sql)
        install_one_db(mysqlcfg, cursor, account, dbname.name, local_path, numprocs,
                       force_overwrite=force_overwrite, verbose=verbose,
                       debug=debug)
        cursor.close()
    
    
    if debug:
        display_dbs(cursor, release)
        print(server)
    
    cursor.close()

@main.command()
@_cfgpath
@_mysqlcfg
@_verbose
@_debug
def drop(configpath, mysql, verbose, debug):
    """drop databases from a MySQL server"""
    mysqlcfg = read_mysql_config(mysql, "mysql")
    account = HostAccount(mysqlcfg["host"], mysqlcfg["user"],
                          mysqlcfg["passwd"])
    server = DbConnection(account, db_name='PARENT', pool_recycle=36000)
    cursor = server.cursor()
    release, local_path, species_dbs = read_config(configpath)
    content = os.listdir(local_path)
    dbnames = reduce_dirnames(content, species_dbs)
    for dbname in dbnames:
        print("Dropping %s" % dbname)
        _drop_db(cursor, dbname)

    if verbose:
        display_dbs(cursor, release)
    
    cursor.close()

@main.command()
@_dbrc_out
def exportrc(outpath):
    """exports the rc directory to the nominated path
    
    setting an environment variable ENSEMBLDBRC with this path
    will force it's contents to override the default ensembldb settings"""
    shutil.copytree(ENSEMBLDBRC, outpath)
    print("Contents written to %s" % outpath)
    

if __name__ == "__main__":
    main()
