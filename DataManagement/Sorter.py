#!/usr/bin/python
'''
Run program to unzip and sort data onto airglow's remote2 server

History: 25 Aug 2011 - initial script written (PERL)
         16 Jul 2013 - rewritten to PYTHON
         12 Feb 2014 - Updated to v3.0 - txtcheck

Written by Daniel J. Fisher (dfisher2@illinois.edu)
'''

# Import required modules
import os
import sys
import traceback
import tarfile
from glob import glob
import datetime as dt
import pytz
from collections import defaultdict
import Emailer
import matplotlib
matplotlib.use('AGG')
import fpiinfo
import asiinfo
import gpsinfo
# Data Processing modules
import FPI
import Image
import FPIprocess
import ASIprocess
import GPSprocess


def instrumentcode():
    '''
    Summary
    -------
        code = instrumentcode()
        Is the dictionary of all full instruments names given the 3 letter abbreviation.
            Contains pointers for cloud & templ data.
    
    Outputs
    ------
        code =  dictionary of all full names from abbr

    History
    -------
        2/25/14 -- Written by DJF (dfisher2@illinois.edu)
    '''

    # Instrument Dictionary  TODO: make all 3 letters or use directory structure
    code = defaultdict(str)
    code['fpi'] = 'minime'
    code['asi'] = 'casi'
    code['nfi'] = 'cnfi'
    code['pic'] = 'picasso'
    code['sky'] = 'skymon'
    code['swe'] = 'swenson'
    code['cas'] = 'cases'
    code['tec'] = 'scinda'
    code['scn'] = 'scintmon'
    code['bwc'] = 'cloudsensor'
    code['x3t'] = 'x300'
    # Pointers for them too
    code['cloud'] = 'bwc'
    code['templ'] = 'x3t'

    return(code)


def sortinghat(dir_data,f):
    '''
    Summary
    -------
        result = sortinghat(dir_data,f,code):
        Concatinates/Unzips and moves data to folder. Returns errors and filenames.

    Inputs
    ------
        dir_data = directory that data will be placed in
        f = glob'd info file that contains name, parts, and size

    Outputs
    -------
        result = names - successful sort, else - error

    History
    -------
        7/18/13 -- Written by DJF (dfisher2@illionis.edu)
    '''
    
    result = 0
    code = instrumentcode()
    print "!!! Begin Sorting..."
    # Read info file
    info = open(f, 'r')
    zelda = info.readline().rstrip().split('.tar.gz',1)[0]+'.tar.gz'
    parts = int(info.readline())
    tsize = int(info.readline())
    time = dt.datetime.strptime(info.readline()[:19],'%Y-%m-%d %H:%M:%S')
    info.close()
    # Check all parts present
    print 'Parts:',len(glob(zelda + '*')),'/',parts
    ## Case 1 - no data created last night
    if(tsize ==0 and parts == 0):
        ## Emails Warning that system is down!
        print '!!! No Data Collected'
        if '' == code[zelda[0:3]]:
            msg = "%s down at %s!\nInternet & Sortinghat are working, instrument issue." %(zelda[0:5],zelda[6:9])
        else:
            msg = "%s%s down at %s!\nIs it a full moon?\nInternet & Sortinghat are working, instrument issue." %(code[zelda[0:3]],zelda[3:5],zelda[6:9])
        subject = "!!! No data collected on %02s-%02s-%02s" %(zelda[14:16],zelda[16:18],zelda[12:14])
        Emailer.emailerror(subject,msg)
        # Move info file to tracking folder
        os.system('mv ' + f + ' ./tracking')
    ## Case 2 - all parts sent over in rx
    elif len(glob(zelda + '*')) == parts:
        # Check that folder to data exists
        try:
            os.makedirs(dir_data)
            os.system('chmod u+rwx,go+rX,go-w ' +dir_data)
            print "!!! Folder Created - Verify..."
        except OSError:
            print '!!! Folders Exist... moving on'
        # Concatinate the split files
        oscar = glob(zelda+'*')
        oscar.sort()
        print "\n".join(str(x) for x in oscar)
        os.system('cat ' + zelda + '* > temp.tar.gz')
        os.system('chmod 770 temp.tar.gz')
        # Check that size is correct
        statinfo = os.stat("temp.tar.gz")
        print 'Sizes:',statinfo.st_size,'/',tsize
        if statinfo.st_size == tsize:
            # Untar the gunzip files 
            tar = tarfile.open("temp.tar.gz","r:gz")
            try:
                result = tar.getnames()
                tar.extractall(dir_data)
                os.system('mv ' + f + ' ./tracking')
            except:
                age = (dt.datetime.utcnow()-time).total_seconds()/3600.0
                subject = "!!! Extract Error on %02s-%02s-%02s" %(zelda[14:16],zelda[16:18],zelda[12:14])
                print subject
                msg = "%s%s issue at %s!\nThis file will not untar.\nBad Zip? Try -p %i" %(code[zelda[0:3]],zelda[3:5],zelda[6:9],age/24)
                Emailer.emailerror(subject,msg)
                result = []
            tar.close()
        else:
            print '!!! Waiting for complete parts...'
    ## Case 3 - all parts not yet sent
    else:
        print '!!! Waiting for parts...'
    os.system('rm -f temp.tar.gz')
    return(result)
    
    
def makeinfo(f):
    '''
    Summary
    -------
        makeinfo(f):
        Takes f (.tar.gz) and creates an appropriate txt info file.

    Inputs
    ------
        f = name of an unzipped tarball file (not standard Zip->Send->Sort)

    History
    -------
        2/13/14 -- Written by DJF (dfisher2@illinois.edu)
    '''
    
    now = dt.datetime.utcnow()
    statinfo = os.stat(f)
    f = f.lower()
    if len(f) == 25:
        check = open(f[0:-7]+'.txt', 'w')
    else:
        check = open(f[0:-4]+'.txt', 'w')
    check.write(f + '\n' + '1\n' + str(statinfo.st_size) +'\n' + str(now) + '\n999')
    check.close()
    return
    
    
if __name__=="__main__":

    # The location of programs (needed since running in crontab)
    dir_local = '/rdata/airglow/'
    #dir_script = '/usr/local/share/airglowrsss/Python/Programs/'
    #python = '/usr/local/python/'
    dir_share = '/rdata/airglow/share/'
    
    print "\n!!!!!!!!!!!!!!!!!!!!"
    print '!!! BEGIN TIMESTAMP:',dt.datetime.now()

    # Close Program if already running (just in case...)
    pid = str(os.getpid())
    pidfile = "/tmp/Sorter.pid"
    if os.path.isfile(pidfile):
        print "%s already exists, exiting" % pidfile
        sys.exit()
    else:
        file(pidfile, 'w').write(pid)
        
    # Load instrument dictionary
    code = instrumentcode()
    # Set order so bwc & x3t process first
    ids = ['Cloud','TempL']
    ids.extend(code.keys())
   
    
    # TRY YOUR HARDEST
    try:
        # Get Data in RX folder
        os.chdir(dir_local+'rx/')
        #os.system('chmod 774 *') No longer have permissions, tx sends as 774.
        # Get info files for non-standard (Zip->Send->Sort) data
        rxfiles = ["fpi04_kaf", "fpi9", "cas01_hka"]
        for x in rxfiles:
            for files in glob(x + '*'):
                makeinfo(files)
        # Go through all txt files to Sort data
        for i in ids:
            for f in glob(i + '*.txt'):
                # Get information for assembling & sos this unAmericarting file
                name = f[0:18]              # name         = IIIII_SSS_YYYYMMDD
                instrument = f[0:5].lower() # instrument   = IIIII
                instr = f[0:3].lower()      # instrument   = III__
                inum = f[3:5]               # instrument # = ___II
                site = f[6:9].lower()       # site         = SSS
                # FOR OLDER FILES THAT DID DOY, ELSE STANDARD DAY
                if f[17] in ['.']:
                    date = f[10:17]             # date         = YYYYDDD
                    dates = f[12:17]            # dates        = YYDDD
                    year = int(f[10:14])        # year         = YYYY
                    doy = int(f[14:17])         # doy          = DDD
                    dn = dt.datetime(year,1,1)+dt.timedelta(days = doy-1)
                    month = dn.timetuple().tm_mon
                    day = dn.timetuple().tm_mday
                else:
                    date = f[10:18]             # date         = YYYYMMDD
                    dates = f[12:18]            # dates        = YYMMDD
                    year = int(f[10:14])        # year         = YYYY
                    month = int(f[14:16])       # month        = MM
                    day = int(f[16:18])         # day          = DD
                    dn = dt.datetime(year,month,day)
                    doy = dn.timetuple().tm_yday
                print "\n!!! For", name
                # Fix inum for Letters
                if inum[1].isalpha():
                    inum = inum[1]


                ##### TEMPLOG CASE: #####
                if instrument in ['cloud', 'templ']:
                    ### Part 1: Sorting Data
                    print "!!! Begin Sorting..."
                    # Create fake checksum for tracker
                    checkname = code[instrument]+'00'+f[5:]
                    os.system('cp '+f+' '+checkname)
                    makeinfo(checkname)
                    os.rename(checkname,'tracking/'+checkname)
                    # Move data into directory
                    dir_data = dir_local + 'templogs/' + code[code[instrument]] + '/' + site + '/'
                    os.rename(f, dir_data + f)
                    os.system('chmod 744 ' + dir_data + f)
                    print "!!! Success Sorting"
                    
                elif instr in ['bwc', 'x3t']:
                    ### Send Error if checkfile
                    print "!!! Begin Sorting..."
                    sortinghat([],f)


                ##### FPI CASE: #####
                elif instr in 'fpi':
                    ### Part 1: Sorting Data
                    dir_data = dir_local + 'fpi/' + code[instr] + inum + '/' + site + '/' + str(year) + '/'
                    result = sortinghat(dir_data,f)
                    if result:
                        # CHMOD all added files
                        for r in result:
                            os.system('chmod u+rwx,go+rX,go-w ' + dir_data + r)
                            os.system('chown airglow.fpi ' + dir_data + r)
                        # Remove files from rx
                        os.system('rm -f ' + name + '*')
                        print "!!! Success Sorting"
                        
                    ### Part 2: Processing Data
                        print "!!! Begin Processing..."
                        # Get correct doy from files
                        for r in result:
                            if r[-4:] in '.img':
                                ldn = FPI.ReadIMG(dir_data+r).info['LocalTime']
                                if ldn.hour < 12:
                                    ldn -= dt.timedelta(days = 1)
                                doy = ldn.timetuple().tm_yday
                                year = ldn.year
                                break
                        # Run processing script for site
                        try:
                            warning = FPIprocess.process_instr(code[instr] + inum,year,doy)
                            if warning:
                                subject = "!!! Manually inspect (\'" + code[instr]+inum+'\','+str(year)+','+str(doy)+') @ ' + site
                                print subject
                                Emailer.emailerror(subject, warning)
                        except:
                            subject = "!!! Processing error (\'" + code[instr]+inum+'\','+str(year)+','+str(doy)+') @ ' + site
                            print subject
                            Emailer.emailerror(subject, traceback.format_exc())
                        # Run CV processing for project
                        # ?????
                        print "!!! End Processing"


                ##### GPS CASE: #####
                elif instr in ['tec','scn']:
                    ## Part 1: Sorting Data
                    dir_data = dir_local + 'gps/' + code[instr] + inum + '/' + site + '/' + str(year) + '/'
                    # if SCN - Send data to raw folder
                    if instr == 'scn':
                        dir_data = dir_data + '/raw_data/'
                        try:
                            os.makedirs(dir_data)
                            os.system('chmod 755 ' + dir_data)
                        except OSError:
                            print '!!! Raw Folder Exists... moving on'
                    result = sortinghat(dir_data,f)
                    if result:
                        # CHMOD all added files
                        for r in result:
                            os.system('chmod u+rwx,go+rX,go-w ' + dir_data + r)
                            os.system('chown airglow.gps ' + dir_data + r)
                        # Remove files from rx
                        os.system('rm -f ' + name + '*')

                        print "!!! Success Sorting"
                        
                    ### Part 2: Processing Data
                        print "!!! Begin Processing..."
                        # Run processing script for site
                        try:
                            GPSprocess.process_instr(code[instr] + inum,year,doy)
                        except:
                            subject = "!!! Processing error (\'" + code[instr]+inum+'\','+str(year)+','+str(doy)+') @ ' + site
                            print subject
                            Emailer.emailerror(subject, traceback.format_exc())
                        print "!!! End Processing"


                ##### CASES CASE: #####
                elif instr in ['tec','scn','cas']:
                    ## Part 1: Sorting Data
                    dir_data = dir_local + 'gps/' + code[instr] + inum + '/' + site + '/' + str(year) + '/'
                    # Move info file to tracking
                    os.system('mv ' + f + ' ./tracking')
                    # Move data to proper folder
                    os.system('cp ' + name + '* ' + dir_data)
                    # Change ownership
                    os.system('chmod u+rwx,go+rX,go-w ' + dir_data + name + '*')
                    os.system('chown airglow.gps ' + dir_data + name + '*')
                    # Remove files from rx
                    os.system('rm -f ' + name + '*')
                    print "!!! Success Sorting"
                        
                        
                
                ##### IMAGER CASE: #####
                elif instr in ['asi','nfi','pic','sky','swe']:
                    ### Part 1: Sorting Data
                    dir_data = dir_local + 'imaging/' + code[instr] + inum + '/' + site + '/' + str(year) + '/'
                    result = sortinghat(dir_data,f)
                    if result:
                        if asiinfo.get_site_info(site)['share']:
                            # Check that share folder for copy exists
                            dir_copy = dir_share + site + '/' + str(year) + '/' + str(doy) + '/'
                            try:
                                os.makedirs(dir_copy)
                                os.system('chmod 755' + dir_copy)
                                print "!!! Share Folder Created"
                            except OSError:
                                print '!!! Share Folder Exists... moving on'
                        # CHMOD all added files
                        for r in result:
                            os.system('chmod u+rwx,go+rX,go-w ' + dir_data + r)
                            os.system('chown airglow.imaging ' + dir_data + r)
                            #os.system('mv ' + dir_data + r + ' ' + dir_data + str(doy) + '/.')
                            # Copy files if needed
                            if asiinfo.get_site_info(site)['share']:
                                os.system('cp -r ' + dir_data + r + ' ' + dir_copy)
                        # Remove files from rx
                        os.system('rm -f ' + name + '*')
                        print "!!! Success Sorting"
                        
                    ### Part 2: Processing Data
                        print "!!! Begin Processing..."
                        ## Get correct doy from files
                        #for r in result:
                        #    if r[-4:] in '.tif':
                        #        ldn = Image.open(dir_data+r).info['UniversalTime'] # Local is standard, but ASI is JJM's timechoice
                        #        if ldn.hour < 12:
                        #            ldn -= dt.timedelta(days = 1)
                        #        doy = ldn.timetuple().tm_yday
                        #        year = ldn.year
                        #        break
                        # Run processing script for site
                        # TODO: Mimic FPIprocess warnings
                        msg = ASIprocess.process_instr(code[instr] + inum,year,doy)
                        if msg:
                            subject = "!!! Processing error (\'" + code[instr]+inum+'\','+str(year)+','+str(doy)+') @ ' + site
                            print subject
                            Emailer.emailerror(subject, msg)
                        print "!!! End Processing"
                        
                        
                ##### BAD INSTR CATCH #####
                else:
                    subject = "!!! Badly named files: " + name
                    print subject
                    Emailer.emailerror(subject, 'Name is not real instrument...')

    except:
        subject = "!!! Something is wrong..."
        print subject
        Emailer.emailerror(subject, traceback.format_exc())
        
    finally:
        print "\n!!! Unpack Complete!"
        os.unlink(pidfile)
    