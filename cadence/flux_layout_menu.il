; Portable layout-menu entry. Loading this file does not modify .cdsinit.
fluxLayoutPluginFile = simplifyFilename(get_filename(piport))
fluxLayoutPluginParts = parseString(fluxLayoutPluginFile "/")
fluxLayoutPluginDir = strcat("/" buildString(reverse(cdr(reverse(fluxLayoutPluginParts))) "/"))
fluxLayoutLauncher = strcat(fluxLayoutPluginDir "/launch_from_layout.sh")
unless(boundp('fluxLayoutRegisteredApps) fluxLayoutRegisteredApps = nil)
unless(boundp('fluxLayoutLaunchJobs) fluxLayoutLaunchJobs = makeTable('fluxLayoutLaunchJobs nil))

procedure(fluxLayoutShellQuote(value)
  let((quoted index character)
    quoted = "'"
    for(index 1 strlen(value)
      character = substring(value index 1)
      quoted = strcat(quoted if(character == "'" then "'\\''" else character))
    )
    strcat(quoted "'")
  )
)

procedure(fluxLayoutMappingFile()
  let((candidate result)
    result = nil
    when(isCallable('ddGetUpdatedLib)
      candidate = ddGetUpdatedLib()
      when(stringp(candidate) && strlen(candidate) > 0 && isFile(candidate)
        result = simplifyFilename(candidate)
      )
    )
    unless(result
      when(isCallable('ddGetForcedLib)
        candidate = ddGetForcedLib()
        when(stringp(candidate) && strlen(candidate) > 0 && isFile(candidate)
          result = simplifyFilename(candidate)
        )
      )
    )
    unless(result
      when(isCallable('ddGetStartup)
        candidate = ddGetStartup("cds.lib")
        when(stringp(candidate) && strlen(candidate) > 0 && isFile(candidate)
          result = simplifyFilename(candidate)
        )
      )
    )
    result
  )
)

procedure(fluxLayoutWriteContext()
  let((window cv lib viewObj tf mapping temporary directory filename port entry variable)
    window = hiGetCurrentWindow()
    cv = geGetEditCellView(window)
    unless(cv error("Flux: invoke this command from a layout editor window.\n"))
    lib = ddGetObj(cv~>libName)
    viewObj = ddGetObj(cv~>libName cv~>cellName cv~>viewName)
    tf = techGetTechFile(cv)
    mapping = fluxLayoutMappingFile()
    unless(lib && viewObj && tf error("Flux: cannot resolve the invoking layout and attached technology.\n"))
    unless(mapping error("Flux: cannot identify an existing loaded library mapping file.\n"))
    temporary = strcat(getTempDir() "/" makeTempFileName("flux_layout_XXXXXX"))
    directory = strcat(temporary "_private")
    unless(system(strcat("/bin/mkdir -m 700 -- " fluxLayoutShellQuote(directory))) == 0
      error("Flux: cannot create a private layout-context directory.\n")
    )
    when(isFile(temporary) deleteFile(temporary))
    filename = strcat(directory "/context.tsv")
    port = outfile(filename)
    unless(port error("Flux: cannot write the private layout context.\n"))
    fprintf(port "TFGEN_LAYOUT_CONTEXT_1\n")
    foreach(entry list(
      list("library" cv~>libName) list("cell" cv~>cellName) list("view" cv~>viewName)
      list("technology" tf~>libName) list("library_path" lib~>readPath)
      list("view_path" viewObj~>readPath) list("workspace" getWorkingDir())
      list("cds_lib" mapping))
      fprintf(port "C\t%L\t%L\n" car(entry) cadr(entry))
    )
    foreach(variable '("PATH" "CDS_LIC_FILE" "LM_LICENSE_FILE" "LD_LIBRARY_PATH"
                       "LD_PRELOAD" "PYTHONHOME" "PYTHONPATH" "TCL_LIBRARY" "TK_LIBRARY" "QT_PLUGIN_PATH"
                       "CDSHOME" "IC_HOME" "CDS_ROOT" "CDS_INST_DIR" "OA_HOME" "CDS_AUTO_64BIT")
      fprintf(port "E\t%L\t%L\n" variable getShellEnvVar(variable))
    )
    close(port)
    unless(system(strcat("/bin/chmod 600 -- " fluxLayoutShellQuote(filename))) == 0
      error("Flux: cannot protect the layout-context file.\n")
    )
    filename
  )
)

procedure(fluxLayoutError(message)
  hiDisplayAppDBox(?name 'fluxLayoutErrorBox ?dboxBanner "Flux Studio"
    ?dboxText message ?dialogType hicErrorDialog ?buttonLayout 'Close)
)

procedure(fluxLayoutReceiveLaunchData(process data)
  let((job buffer lines complete index line prefix resultPrefix statusText status valid characterIndex)
    when(stringp(data) && strlen(data) > 0
      ; The launcher keeps detailed output in its private log. Only its
      ; startup/log-location/failure messages remain on the IPC channel.
      printf("%s" data)
      job = fluxLayoutLaunchJobs[process]
      when(job
        buffer = strcat(job['buffer] data)
        complete = substring(buffer strlen(buffer) 1) == "\n"
        lines = parseString(buffer "\n")
        job['buffer] = ""
        prefix = "Flux launch log: "
        resultPrefix = "Flux launch result: exit="
        when(lines
          for(index 1 length(lines)
            line = nth(index - 1 lines)
            if(index < length(lines) || complete then
              when(strlen(line) > strlen(prefix) && substring(line 1 strlen(prefix)) == prefix
                job['log] = substring(line strlen(prefix) + 1)
              )
              when(strlen(line) > strlen(resultPrefix) && substring(line 1 strlen(resultPrefix)) == resultPrefix
                statusText = substring(line strlen(resultPrefix) + 1)
                valid = strlen(statusText) <= 3
                for(characterIndex 1 strlen(statusText)
                  unless(member(substring(statusText characterIndex 1) list("0" "1" "2" "3" "4" "5" "6" "7" "8" "9")) valid = nil)
                )
                when(valid
                  status = atoi(statusText)
                  when(status >= 0 && status <= 255 job['actual_exit] = status)
                )
              )
            else
              ; IPC chunks may split the log-location line at any character.
              job['buffer] = line
            )
          )
        )
        when(strlen(job['buffer]) > 16384
          job['buffer] = ""
          printf("Flux: ignored an oversized incomplete launcher feedback line.\n")
        )
      )
    )
    t
  )
)

procedure(fluxLayoutLaunchData(process data)
  unless(errset(fluxLayoutReceiveLaunchData(process data) t)
    printf("Flux: launcher-feedback handler failed for %L; inspect the reported launch log.\n" process)
  )
  t
)

procedure(fluxLayoutHandleLaunchExit(process exitStatus)
  let((job log message status)
    job = fluxLayoutLaunchJobs[process]
    log = if(job then job['log] else "See the Flux launch log path printed in the CIW.")
    status = if(job && numberp(job['actual_exit]) then job['actual_exit] else exitStatus)
    when(job && numberp(job['actual_exit])
      printf("Flux launcher completed: exit=%L (IPC status=%L).\n" status exitStatus)
    )
    remove(process fluxLayoutLaunchJobs)
    unless(status == 0
      printf("Flux launch failed: process %L exited with status %L.\nFlux launch log: %s\n" process status log)
      message = sprintf(nil "The layout generator exited with status %L.\n\nLaunch log: %s\n\nSee the CIW and this private launch log for the startup error." status log)
      fluxLayoutError(message)
    )
    t
  )
)

procedure(fluxLayoutLaunchExit(process exitStatus)
  unless(errset(fluxLayoutHandleLaunchExit(process exitStatus) t)
    printf("Flux: launcher-exit handler failed for %L (status %L). See the CIW launch-log path.\n" process exitStatus)
  )
  t
)

procedure(fluxLayoutOpenCurrent()
  let((captured filename command process job parts directory cv started)
    unless(isFile(fluxLayoutLauncher)
      printf("Flux launch failed: missing launcher %s\n" fluxLayoutLauncher)
      fluxLayoutError("The portable Flux launcher is missing beside the menu file.")
      return(nil)
    )
    captured = errset(fluxLayoutWriteContext() t)
    unless(captured
      fluxLayoutError("Cannot capture this layout's context. See the CIW diagnostic; no layout was modified.")
      return(nil)
    )
    filename = car(captured)
    parts = parseString(filename "/")
    directory = strcat("/" buildString(reverse(cdr(reverse(parts))) "/"))
    cv = geGetEditCellView(hiGetCurrentWindow())
    printf("Flux layout source: %s/%s/%s\n" cv~>libName cv~>cellName cv~>viewName)
    printf("Flux layout context: %s\n" filename)
    command = strcat("/bin/bash " fluxLayoutShellQuote(fluxLayoutLauncher) " " fluxLayoutShellQuote(filename))
    started = errset(ipcBeginProcess(command "" 'fluxLayoutLaunchData 'fluxLayoutLaunchData 'fluxLayoutLaunchExit) t)
    process = if(started then car(started) else nil)
    if(process then
      job = makeTable(gensym("fluxLayoutLaunchRecord") nil)
      job['context] = filename
      job['log] = strcat(directory "/launch.log")
      job['buffer] = ""
      fluxLayoutLaunchJobs[process] = job
    else
      printf("Flux launch failed: could not start launcher %s\n" fluxLayoutLauncher)
      fluxLayoutError("Cannot start the Flux launcher. Check the CIW, launcher path and Bash availability.")
    )
    process
  )
)

procedure(fluxLayoutMenuTrigger(_args)
  list(hiCreatePulldownMenu(
    gensym("fluxLayoutMenu") "Flux Studio"
    list(hiCreateMenuItem(?name gensym("fluxLayoutOpen")
      ?itemText "Open RF Layout Generator" ?callback "fluxLayoutOpenCurrent()"))
  ))
)

foreach(fluxLayoutApp deGetAllViewTypes()
  when(rexMatchp("^maskLayout" fluxLayoutApp) && !member(fluxLayoutApp fluxLayoutRegisteredApps)
    deRegUserTriggers(fluxLayoutApp nil 'fluxLayoutMenuTrigger)
    fluxLayoutRegisteredApps = cons(fluxLayoutApp fluxLayoutRegisteredApps)
  )
)
printf("Flux Studio menu registered for %L.\n" reverse(fluxLayoutRegisteredApps))
