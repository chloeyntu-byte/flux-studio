; SNPFit: build only new cells from an explicit fitted circuit.
; Caller provides snpfitLib/snpfitCell/snpfitElements/snpfitPorts/snpfitReference.
unless(boundp('snpfitHasVisual) snpfitHasVisual=nil)
unless(boundp('snpfitMakeSymbolEnabled) snpfitMakeSymbolEnabled=t)
unless(boundp('snpfitCircuitHash) snpfitCircuitHash="")
unless(boundp('snpfitVisualPlanHash) snpfitVisualPlanHash="")
unless(boundp('snpfitVisualWires) snpfitVisualWires=nil)
procedure(snpfitWire(cv points)
  schCreateWire(cv "draw" "full" points 0.0625 0.0625 0.0)
)
procedure(snpfitMid(box)
  ((xCoord(car(box))+xCoord(cadr(box)))/2.0):((yCoord(car(box))+yCoord(cadr(box)))/2.0)
)
procedure(snpfitTerminalPoint(inst termName)
  let((term pin)
    term=car(setof(x inst~>master~>terminals x~>name==termName))
    unless(term error("Missing real master terminal %s/%s\n" inst~>cellName termName))
    pin=car(term~>pins)
    unless(pin error("No pin figure for %s\n" termName))
    dbTransformPoint(snpfitMid(pin~>fig~>bBox) inst~>transform)
  )
)
procedure(snpfitMasterPoint(master termName)
  let((term pin)
    term=car(setof(x master~>terminals x~>name==termName))
    unless(term error("Missing real master terminal %s\n" termName))
    pin=car(term~>pins)
    unless(pin error("Missing real master pin %s\n" termName))
    snpfitMid(pin~>fig~>bBox)
  )
)
procedure(snpfitAxis(p1 p2)
  if(abs(xCoord(p2)-xCoord(p1))>=abs(yCoord(p2)-yCoord(p1)) then
    if(xCoord(p2)>xCoord(p1) then "R0" else "R180")
  else
    if(yCoord(p2)>yCoord(p1) then "R90" else "R270")
  )
)
procedure(snpfitPlacementPoint(inst)
  ; Display labels can change bBox and put its center off the OA grid.
  ; The actual electrical endpoints define a stable R/L/C body anchor.
  if(inst~>cellName=="mind" then inst~>xy else
    snpfitMid(list(snpfitTerminalPoint(inst "PLUS") snpfitTerminalPoint(inst "MINUS")))
  )
)
procedure(snpfitRotationForAxis(kind axis)
  let((master nativeAxis nativeIndex targetIndex)
    if(kind=="mind" then axis else
      master=dbOpenCellViewByType("analogLib" kind "symbol" "" "r")
      unless(master error("Cannot open actual analogLib master %s\n" kind))
      nativeAxis=snpfitAxis(snpfitMasterPoint(master "PLUS") snpfitMasterPoint(master "MINUS"))
      nativeIndex=cond((nativeAxis=="R0" 0) (nativeAxis=="R90" 1) (nativeAxis=="R180" 2) (t 3))
      targetIndex=cond((axis=="R0" 0) (axis=="R90" 1) (axis=="R180" 2) (t 3))
      dbClose(master)
      nth(mod(targetIndex-nativeIndex+4 4) list("R0" "R90" "R180" "R270"))
    )
  )
)
procedure(snpfitLabel(cv wire position name)
  schCreateWireLabel(cv car(wire) position name "lowerLeft" "R0" "stick" 0.0625 nil)
)
procedure(snpfitNetName(name)
  if(!snpfitExportReference && name==snpfitReference then "gnd!" else name)
)
procedure(snpfitStub(cv start finish name)
  let((w)
    w=snpfitWire(cv list(start finish))
    snpfitLabel(cv w finish snpfitNetName(name))
  )
)
procedure(snpfitRouteHasEndpoint(name terminal position)
  let((found points)
    found=nil
    foreach(row snpfitVisualWires
      points=nth(5 row)
      when(nth(1 row)==name && nth(2 row)==terminal &&
           (terminal!="PIN" || equal(position car(points))) found=t)
      when(nth(3 row)==name && nth(4 row)==terminal &&
           (terminal!="PIN" || equal(position car(last(points)))) found=t)
    )
    found
  )
)
procedure(snpfitPin(cv name position direction)
  let((master anchor origin finish)
    master=dbOpenCellViewByType("basic" "iopin" "symbol" "" "r")
    unless(master error("Cannot open basic/iopin/symbol\n"))
    anchor=if(master~>terminals then snpfitMasterPoint(master car(master~>terminals)~>name) else 0.0:0.0)
    origin=(xCoord(position)-xCoord(anchor)):(yCoord(position)-yCoord(anchor))
    unless(schCreatePin(cv master name "inputOutput" nil origin "R0") error("Cannot create external pin %s\n" name))
    finish=cond((direction=="right" (xCoord(position)-0.5):yCoord(position))
                (direction=="top" xCoord(position):(yCoord(position)-0.5))
                (direction=="bottom" xCoord(position):(yCoord(position)+0.5))
                (t (xCoord(position)+0.5):yCoord(position)))
    unless(snpfitHasVisual && snpfitRouteHasEndpoint(name "PIN" position)
      snpfitStub(cv position finish name))
    dbClose(master)
  )
)
procedure(snpfitRouteEndpoint(cv name terminal fallback)
  let((inst term point found)
    cond(
      (terminal=="NET" fallback)
      (terminal=="PIN"
        term=car(setof(v cv~>terminals v~>name==name))
        unless(term error("Missing center-path external terminal %s\n" name))
        found=nil
        foreach(pin term~>pins
          point=snpfitPinAnchor(pin)
          when(abs(xCoord(point)-xCoord(fallback))<0.000001 &&
               abs(yCoord(point)-yCoord(fallback))<0.000001 found=point)
        )
        unless(found error("Center-path physical pin anchor differs %s\n" name))
        found
      )
      (t
        inst=car(setof(v cv~>instances v~>name==name))
        unless(inst error("Missing center-path component %s\n" name))
        unless(member(terminal list("PLUS" "MINUS"))
          error("Unsupported center-path terminal %s\n" terminal))
        snpfitTerminalPoint(inst terminal)
      )
    )
  )
)
procedure(snpfitRoutePoints(cv row)
  let((points start finish result)
    points=copy(nth(5 row))
    unless(length(points)>=2 error("Center-path wire needs two endpoints\n"))
    start=snpfitRouteEndpoint(cv nth(1 row) nth(2 row) car(points))
    finish=snpfitRouteEndpoint(cv nth(3 row) nth(4 row) car(last(points)))
    points=cons(start cdr(points))
    points=reverse(cons(finish cdr(reverse(points))))
    result=nil
    foreach(point points
      unless(result && equal(point car(result)) result=cons(point result)))
    result=reverse(result)
    unless(length(result)>=2 error("Center-path wire collapsed to one point\n"))
    result
  )
)
procedure(snpfitDrawCenterWires(cv)
  let((index segment points wires)
    index=0
    foreach(row snpfitVisualWires
      points=snpfitRoutePoints(cv row)
      wires=snpfitWire(cv points)
      unless(wires error("Cannot create center-path wire %d\n" index))
      snpfitLabel(cv wires car(points) snpfitNetName(car(row)))
      segment=0
      foreach(fig wires
        dbReplaceProp(fig "snpfitVisualKind" "string" "wire")
        dbReplaceProp(fig "snpfitVisualIndex" "int" index)
        dbReplaceProp(fig "snpfitVisualSegment" "int" segment)
        segment=segment+1
      )
      index=index+1
    )
  )
)
procedure(snpfitTerminalStub(cv point other name)
  let((finish)
    finish=if(abs(xCoord(point)-xCoord(other))>=abs(yCoord(point)-yCoord(other)) then
      (xCoord(point)+if(xCoord(point)<xCoord(other) then -0.125 else 0.125)):yCoord(point)
    else
      xCoord(point):(yCoord(point)+if(yCoord(point)<yCoord(other) then -0.125 else 0.125))
    )
    snpfitStub(cv point finish name)
  )
)
procedure(snpfitDrawVisual(cv paths labels)
  let((height fig index)
    ; Device-layer guides and annotations never participate in extraction.
    index=0
    foreach(points paths
      fig=dbCreateLine(cv list("device" "drawing") points)
      unless(fig error("Cannot create winding guide %d\n" index))
      dbReplaceProp(fig "snpfitVisualKind" "string" "path")
      dbReplaceProp(fig "snpfitVisualIndex" "int" index)
      index=index+1
    )
    index=0
    foreach(row labels
      height=cond((member(nth(2 row) list("heading" "rf_title" "metric_title")) 0.375)
                  (member(nth(2 row) list("rf_header" "rf_value" "metric_header" "metric_value")) 0.3125)
                  (member(nth(2 row) list("segment_q" "rf_note" "metric_note")) 0.125)
                  (member(nth(2 row) list("q_table" "q_table_heading" "q_table_row" "panel_heading")) 0.1875)
                  (t 0.1875))
      fig=dbCreateLabel(cv list("device" "drawing") nth(1 row) car(row) nth(3 row) "R0" "roman" height)
      unless(fig error("Cannot create visual annotation %d\n" index))
      dbReplaceProp(fig "snpfitVisualKind" "string" "label")
      dbReplaceProp(fig "snpfitVisualIndex" "int" index)
      index=index+1
    )
  )
)
procedure(snpfitVisualSymbolPin(cv name position direction)
  let((net term fig finish labelPoint just x y)
    x=xCoord(position) y=yCoord(position)
    net=car(setof(n cv~>nets n~>name==name))
    unless(net net=dbCreateNet(cv name))
    term=car(setof(v cv~>terminals v~>name==name))
    unless(term term=dbCreateTerm(net name "inputOutput"))
    fig=dbCreateRect(cv list("pin" "drawing") list((x-0.03125):(y-0.03125) (x+0.03125):(y+0.03125)))
    dbCreatePin(net fig)
    finish=cond((direction=="right" (x-0.375):y) (direction=="top" x:(y-0.375))
                (direction=="bottom" x:(y+0.375)) (t (x+0.375):y))
    labelPoint=cond((direction=="right" (x+0.125):(y+0.0625))
                    (direction=="top" (x+0.125):(y+0.125))
                    (direction=="bottom" (x+0.125):(y-0.1875))
                    (t (x-0.125):(y+0.0625)))
    just=if(direction=="left" then "lowerRight" else "lowerLeft")
    dbCreateLine(cv list("device" "drawing") list(position finish))
    dbCreateLabel(cv list("pin" "label") labelPoint name just "R0" "roman" 0.125)
    term
  )
)
procedure(snpfitPinAnchor(pin)
  let((fig term)
    fig=pin~>fig
    if(fig~>objType=="inst" then
      term=car(fig~>master~>terminals)
      if(term then snpfitTerminalPoint(fig term~>name) else fig~>xy)
    else snpfitMid(fig~>bBox))
  )
)
procedure(snpfitCheckVisual(cv pins prefix)
  let((fp term found actual requested count row inst center p1 p2 axis order paths labels shapes index fig points segment total)
    fp=outfile(snpfitVisualReceiptFile "a")
    count=0
    foreach(term cv~>terminals count=count+length(term~>pins))
    unless(count==length(pins) close(fp) error("Saved %s physical pin count differs\n" prefix))
    foreach(row pins
      term=car(setof(v cv~>terminals v~>name==car(row)))
      unless(term close(fp) error("Saved visual terminal missing %s\n" car(row)))
      requested=nth(1 row) found=nil
      foreach(pin term~>pins
        actual=snpfitPinAnchor(pin)
        when(abs(xCoord(actual)-xCoord(requested))<0.000001 && abs(yCoord(actual)-yCoord(requested))<0.000001 found=actual)
      )
      unless(found close(fp) error("Saved visual anchor differs %s/%s\n" prefix car(row)))
      fprintf(fp "%s_PIN %s %.12g %.12g\n" prefix car(row) xCoord(found) yCoord(found))
    )
    order=if(snpfitExportReference then append(snpfitPorts list(snpfitReference)) else snpfitPorts)
    unless(equal(cv~>portOrder order) close(fp) error("Saved visual portOrder differs\n"))
    fprintf(fp "%s_ORDER %L\n" prefix cv~>portOrder)
    paths=if(prefix=="SCHEMATIC" then snpfitVisualPaths else snpfitSymbolPaths)
    labels=if(prefix=="SCHEMATIC" then snpfitVisualLabels else snpfitSymbolLabels)
    shapes=setof(v cv~>shapes v~>snpfitVisualKind=="path")
    unless(length(shapes)==length(paths) close(fp) error("Saved winding guide count differs\n"))
    index=0
    foreach(points paths
      fig=car(setof(v shapes v~>snpfitVisualIndex==index))
      unless(fig && equal(fig~>points points) close(fp) error("Saved winding guide differs %d\n" index))
      index=index+1
    )
    shapes=setof(v cv~>shapes v~>snpfitVisualKind=="label")
    unless(length(shapes)==length(labels) close(fp) error("Saved annotation count differs\n"))
    index=0
    foreach(row labels
      fig=car(setof(v shapes v~>snpfitVisualIndex==index))
      unless(fig && fig~>theLabel==car(row) && equal(fig~>xy nth(1 row))
        close(fp) error("Saved visual annotation differs %d\n" index))
      index=index+1
    )
    fprintf(fp "%s_GRAPHICS %d %d\n" prefix length(paths) length(labels))
    when(prefix=="SCHEMATIC"
      total=0
      shapes=setof(v cv~>shapes v~>snpfitVisualKind=="wire")
      index=0
      foreach(row snpfitVisualWires
        points=snpfitRoutePoints(cv row)
        segment=0
        while(cdr(points)
          fig=car(setof(v shapes v~>snpfitVisualIndex==index && v~>snpfitVisualSegment==segment))
          ; Native wire figures may normalize endpoint order.  A wire is
          ; undirected; terminal polarity is checked on the real instances.
          unless(fig && (equal(fig~>points list(car(points) cadr(points))) ||
                         equal(reverse(fig~>points) list(car(points) cadr(points))))
            close(fp) error("Saved center-path wire geometry differs %d/%d\n" index segment))
          unless(fig~>net && fig~>net~>name==snpfitNetName(car(row))
            close(fp) error("Saved center-path wire net differs %d/%d\n" index segment))
          fprintf(fp "SCHEMATIC_WIRE %d %d %s %.12g %.12g %.12g %.12g\n"
            index segment fig~>net~>name xCoord(car(fig~>points)) yCoord(car(fig~>points))
            xCoord(cadr(fig~>points)) yCoord(cadr(fig~>points)))
          points=cdr(points) segment=segment+1 total=total+1
        )
        index=index+1
      )
      unless(length(shapes)==total close(fp) error("Saved center-path wire count differs\n"))
      fprintf(fp "SCHEMATIC_WIRES %d %d\n" length(snpfitVisualWires) total)
      foreach(row snpfitVisualPlacements
        inst=car(setof(v cv~>instances v~>name==car(row)))
        unless(inst close(fp) error("Saved visual instance missing %s\n" car(row)))
        center=snpfitPlacementPoint(inst)
        unless(abs(xCoord(center)-xCoord(nth(1 row)))<0.000001 && abs(yCoord(center)-yCoord(nth(1 row)))<0.000001
          close(fp) error("Saved visual component center differs %s\n" car(row)))
        axis=if(inst~>cellName=="mind" then "NA" else snpfitAxis(snpfitTerminalPoint(inst "PLUS") snpfitTerminalPoint(inst "MINUS")))
        unless(axis=="NA" || axis==nth(2 row) close(fp) error("Saved visual component axis differs %s\n" car(row)))
        fprintf(fp "SCHEMATIC_COMPONENT %s %.12g %.12g %s %s\n" car(row) xCoord(center) yCoord(center) inst~>orient axis)
        unless(axis=="NA"
          p1=snpfitTerminalPoint(inst "PLUS") p2=snpfitTerminalPoint(inst "MINUS")
          fprintf(fp "SCHEMATIC_TERMINAL %s PLUS %.12g %.12g\n" car(row) xCoord(p1) yCoord(p1))
          fprintf(fp "SCHEMATIC_TERMINAL %s MINUS %.12g %.12g\n" car(row) xCoord(p2) yCoord(p2))
        )
      )
    )
    close(fp)
    t
  )
)
procedure(snpfitGround(cv position)
  let((inst p)
    inst=dbCreateInstByMasterName(cv "analogLib" "gnd" "symbol" "GND_REF" position "R0")
    p=snpfitTerminalPoint(inst "gnd!")
    snpfitStub(cv p (xCoord(p)+0.5):yCoord(p) snpfitReference)
  )
)
procedure(snpfitSymbolPin(cv name x y side)
  let((net term fig sx tx just)
    sx=if(side=="left" then x+0.375 else x-0.375)
    tx=if(side=="left" then x+0.4375 else x-0.4375)
    just=if(side=="left" then "lowerLeft" else "lowerRight")
    net=dbCreateNet(cv name)
    term=dbCreateTerm(net name "inputOutput")
    fig=dbCreateRect(cv list("pin" "drawing") list((x-0.025):(y-0.025) (x+0.025):(y+0.025)))
    dbCreatePin(net fig)
    dbCreateLine(cv list("device" "drawing") list(x:y sx:y))
    dbCreateLabel(cv list("pin" "label") tx:(y+0.0625) name just "R0" "roman" 0.0625)
    term
  )
)
procedure(snpfitMakeSymbol()
  let((cv names index rows h y left row low high)
    when(ddGetObj(snpfitLib snpfitCell "symbol") error("Refusing to overwrite an existing symbol view\n"))
    cv=dbOpenCellViewByType(snpfitLib snpfitCell "symbol" "schematicSymbol" "w")
    unless(cv error("Cannot create new symbol\n"))
    names=if(snpfitExportReference then append(snpfitPorts list(snpfitReference)) else snpfitPorts)
    if(snpfitHasVisual then
      snpfitDrawVisual(cv snpfitSymbolPaths snpfitSymbolLabels)
      foreach(row snpfitSymbolPins snpfitVisualSymbolPin(cv car(row) nth(1 row) nth(2 row)))
      low=car(snpfitSymbolBounds) high=cadr(snpfitSymbolBounds)
      dbCreateLabel(cv list("instance" "label") xCoord(low):(yCoord(high)+0.375) "[@instanceName]" "lowerLeft" "R0" "roman" 0.1875)
      dbCreateLabel(cv list("device" "label") xCoord(low):(yCoord(low)-0.375) snpfitCell "lowerLeft" "R0" "roman" 0.125)
    else
      rows=ceiling(length(names)/2.0)
      h=max(1.0 (rows+1)*0.375)
      dbCreateRect(cv list("instance" "drawing") list(0.375:(-h) 3.625:0.375))
      dbCreateLabel(cv list("device" "label") 2.0:(-h/2.0) "SNP RLC(K)" "centerCenter" "R0" "roman" 0.125)
      dbCreateLabel(cv list("instance" "label") 0.5:0.5 "[@instanceName]" "lowerLeft" "R0" "roman" 0.0625)
      dbCreateLabel(cv list("device" "label") 0.5:(-h-0.25) snpfitCell "lowerLeft" "R0" "roman" 0.0625)
      index=0
      foreach(name names
        left=(index<rows)
        y=0.0-mod(index rows)*0.375
        snpfitSymbolPin(cv name if(left then 0.0 else 4.0) y if(left then "left" else "right"))
        index=index+1
      )
    )
    dbReplaceProp(cv "portOrder" "ILList" names)
    dbReplaceProp(cv "snpfitGenerated" "string" "SNPFit")
    dbReplaceProp(cv "snpfitCircuitHash" "string" snpfitCircuitHash)
    dbReplaceProp(cv "snpfitVisualPlanHash" "string" snpfitVisualPlanHash)
    dbSave(cv)
    dbClose(cv)
  )
)
procedure(snpfitCheckConnectivity(cv)
  let((row kind inst term n1 n2 actual1 actual2 fp)
    fp=outfile(snpfitConnectivityFile)
    fprintf(fp "SNPFIT_PORTS %L\n" cv~>terminals~>name)
    foreach(row snpfitElements
      kind=nth(1 row)
      inst=car(setof(x cv~>instances x~>name==car(row)))
      unless(inst error("Missing saved instance %s\n" car(row)))
      if(kind=="mind" then
        unless(inst~>ind1==nth(2 row) && inst~>ind2==nth(3 row) error("Saved mutual references differ\n"))
        fprintf(fp "MIND %s %s %s %s\n" inst~>name inst~>ind1 inst~>ind2 inst~>k)
      else
        n1=snpfitNetName(nth(2 row))
        n2=snpfitNetName(nth(3 row))
        actual1=car(setof(x inst~>instTerms x~>name=="PLUS"))~>net~>name
        actual2=car(setof(x inst~>instTerms x~>name=="MINUS"))~>net~>name
        unless(actual1==n1 && actual2==n2 error("Saved connectivity differs: %s %s/%s expected %s/%s\n" inst~>name actual1 actual2 n1 n2))
        fprintf(fp "DEVICE %s %s %s %s %s\n" inst~>name kind actual1 actual2 nth(4 row))
      )
    )
    close(fp)
    t
  )
)
procedure(snpfitBuild()
  let((lib cv names i row kind inst p1 p2 x y value prop params allpins placement center orient)
    lib=ddGetObj(snpfitLib)
    unless(lib error("Private cds.lib did not map target library %s\n" snpfitLib))
    when(ddGetObj(snpfitLib snpfitCell) error("Refusing to overwrite existing cell %s/%s\n" snpfitLib snpfitCell))
    cv=dbOpenCellViewByType(snpfitLib snpfitCell "schematic" "schematic" "w")
    unless(cv error("Cannot create new schematic\n"))
    if(snpfitHasVisual then
      snpfitDrawVisual(cv snpfitVisualPaths snpfitVisualLabels)
      foreach(row snpfitVisualPins snpfitPin(cv car(row) nth(1 row) nth(2 row)))
      unless(snpfitExportReference
        snpfitGround(cv (xCoord(car(snpfitVisualBounds))-1.0):(yCoord(car(snpfitVisualBounds))-1.0)))
    else
      dbCreateLabel(cv list("device" "drawing") 0.0:1.25 snpfitTitle "lowerLeft" "R0" "roman" 0.125)
      dbCreateLabel(cv list("device" "drawing") 0.0:0.875 "Named wire stubs connect equal net labels; reference follows the input SNP." "lowerLeft" "R0" "roman" 0.0625)
      i=0
      foreach(name snpfitPorts
        snpfitPin(cv name -2.5:(-i*0.75) "left")
        i=i+1
      )
      if(snpfitExportReference then snpfitPin(cv snpfitReference -2.5:(-i*0.75) "left") else snpfitGround(cv -2.0:(-i*0.75)))
    )
    i=0
    foreach(row snpfitElements
      kind=nth(1 row)
      x=1.0+floor(i/10.0)*5.0
      y=-mod(i 10)*1.875
      value=nth(4 row)
      prop=cond((kind=="res" "r") (kind=="cap" "c") (kind=="ind" "l") (kind=="mind" "k") (t error("Unknown kind\n")))
      params=list(list(prop "string" value))
      when(kind=="mind" params=append(params list(list("ind1" "string" nth(2 row)) list("ind2" "string" nth(3 row)))))
      orient="R90"
      when(snpfitHasVisual
        placement=car(setof(v snpfitVisualPlacements car(v)==car(row)))
        unless(placement error("Missing visual placement %s\n" car(row)))
        orient=snpfitRotationForAxis(kind nth(2 placement))
        x=0.0 y=0.0
      )
      inst=dbCreateParamInstByMasterName(cv "analogLib" kind "symbol" car(row) x:y orient 1 params)
      unless(inst error("Failed placing %s\n" car(row)))
      when(snpfitHasVisual
        center=snpfitPlacementPoint(inst)
        inst~>xy=(xCoord(nth(1 placement))-xCoord(center)):(yCoord(nth(1 placement))-yCoord(center))
        x=xCoord(nth(1 placement)) y=yCoord(nth(1 placement))
      )
      if(kind=="mind" then
        dbCreateLabel(cv list("device" "drawing") (x-0.5):(y-0.5) strcat(nth(2 row) " <-> " nth(3 row)) "lowerLeft" "R0" "roman" 0.0625)
      else
        p1=snpfitTerminalPoint(inst "PLUS")
        p2=snpfitTerminalPoint(inst "MINUS")
        if(snpfitHasVisual then
          unless(snpfitRouteHasEndpoint(car(row) "PLUS" p1)
            snpfitTerminalStub(cv p1 p2 nth(2 row)))
          unless(snpfitRouteHasEndpoint(car(row) "MINUS" p2)
            snpfitTerminalStub(cv p2 p1 nth(3 row)))
        else
          snpfitStub(cv p1 (xCoord(p1)-0.625):yCoord(p1) nth(2 row))
          snpfitStub(cv p2 (xCoord(p2)+0.625):yCoord(p2) nth(3 row))
        )
      )
      i=i+1
    )
    when(snpfitHasVisual snpfitDrawCenterWires(cv))
    allpins=if(snpfitExportReference then append(snpfitPorts list(snpfitReference)) else snpfitPorts)
    dbReplaceProp(cv "portOrder" "ILList" allpins)
    dbReplaceProp(cv "snpfitGenerated" "string" "SNPFit")
    dbReplaceProp(cv "snpfitCircuitHash" "string" snpfitCircuitHash)
    dbReplaceProp(cv "snpfitVisualPlanHash" "string" snpfitVisualPlanHash)
    schCheck(cv)
    unless(integerp(cv~>connectivityLastUpdated) error("Schematic extraction failed; see check diagnostics\n"))
    dbSave(cv)
    dbClose(cv)
    cv=dbOpenCellViewByType(snpfitLib snpfitCell "schematic" "" "r")
    snpfitCheckConnectivity(cv)
    unless(sort(cv~>terminals~>name 'alphalessp)==sort(copy(allpins) 'alphalessp) error("Saved external pins differ\n"))
    when(snpfitHasVisual snpfitCheckVisual(cv snpfitVisualPins "SCHEMATIC"))
    dbClose(cv)
    when(snpfitMakeSymbolEnabled
      snpfitMakeSymbol()
      cv=dbOpenCellViewByType(snpfitLib snpfitCell "symbol" "" "r")
      unless(sort(cv~>terminals~>name 'alphalessp)==sort(copy(allpins) 'alphalessp) error("Saved symbol external pins differ\n"))
      when(snpfitHasVisual snpfitCheckVisual(cv snpfitSymbolPins "SYMBOL"))
      printf("SNPFIT_SYMBOL_PORTS %L\n" cv~>terminals~>name)
      dbClose(cv)
    )
    printf("SNPFIT_NATIVE_CREATED %s %s\n" snpfitLib snpfitCell)
    t
  )
)
procedure(snpfitAppendSymbol()
  let((cv names)
    unless(snpfitHasVisual error("A visual plan is required for independent symbol creation\n"))
    unless(ddGetObj(snpfitLib snpfitCell "schematic") error("Generate the schematic first\n"))
    when(ddGetObj(snpfitLib snpfitCell "symbol") error("Refusing to overwrite an existing symbol view\n"))
    names=if(snpfitExportReference then append(snpfitPorts list(snpfitReference)) else snpfitPorts)
    cv=dbOpenCellViewByType(snpfitLib snpfitCell "schematic" "" "r")
    unless(cv error("Cannot reopen the generated schematic\n"))
    unless(cv~>snpfitGenerated=="SNPFit" && cv~>snpfitCircuitHash==snpfitCircuitHash && cv~>snpfitVisualPlanHash==snpfitExpectedVisualHash
      error("The schematic is not the matching tool-created visual model\n"))
    unless(sort(cv~>terminals~>name 'alphalessp)==sort(copy(names) 'alphalessp) && equal(cv~>portOrder names)
      error("Existing schematic terminal contract differs\n"))
    dbClose(cv)
    snpfitMakeSymbol()
    cv=dbOpenCellViewByType(snpfitLib snpfitCell "symbol" "" "r")
    unless(sort(cv~>terminals~>name 'alphalessp)==sort(copy(names) 'alphalessp)
      error("Saved symbol terminal contract differs\n"))
    snpfitCheckVisual(cv snpfitSymbolPins "SYMBOL")
    dbClose(cv)
    printf("SNPFIT_SYMBOL_CREATED %s %s\n" snpfitLib snpfitCell)
    t
  )
)
