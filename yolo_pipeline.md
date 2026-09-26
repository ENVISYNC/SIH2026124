ENVISYNC — Full Pipeline Pseudocode
Edge AI + Cloud Urban Intelligence Platform

════════════════════════════════════════
STAGE 1: EDGE INFERENCE (On-Bus, Jetson)
════════════════════════════════════════

INPUT: Video stream from bus cameras (front, rear, side, cabin)
       GPS coordinates (real-time)
       IMU data (speed, acceleration, vibration)

FOR each frame in video_stream:

  STEP 1: Preprocess frame
    - Resize to model input resolution (640x640)
    - Normalize pixel values to [0, 1]
    - Apply noise reduction if low-light conditions detected

  STEP 2: Run quantized YOLO inference (INT8, TensorRT optimized)
    - Model A: Road defect detector
        detects: potholes, cracks, waterlogging, damaged road surface
        trained on: RDD2022 dataset (47,420 images, 55,000+ annotations)
    - Model B: Road sign and hazard detector
        detects: damaged signs, construction zones, school/hospital zones
        trained on: IDD dataset (46,588 images, 34 classes)
    - Model C : Vehicle and pedestrian detector
        for congestion scoring and incident detection

  STEP 3: Filter detections
    - Drop detections below confidence threshold (default: 0.60)
    - Apply Non-Maximum Suppression to remove overlapping boxes
    - Validate with IMU: discard detections during high-vibration frames
      (bus hitting a speed bump ≠ pothole)

  STEP 4: Build event object
    event = {
      type: "pothole" | "crack" | "waterlogging" | "damaged_sign" | ...,
      confidence: float,
      bbox: [x, y, w, h],
      gps: {lat, lon},
      speed_kmh: from IMU,
      timestamp: UTC,
      bus_id: string,
      frame_crop: base64 image patch,
      signature: HMAC hash (tamper-proof)
    }

  STEP 5: Local buffer
    - Append event to local SQLite queue
    - If connectivity available: attempt immediate sync to cloud
    - If no connectivity: store locally, sync when signal returns
      (store-and-forward, zero data loss guarantee)

════════════════════════════════════════
STAGE 2: CLOUD PROCESSING (FastAPI + Kafka)
════════════════════════════════════════

ON event received from bus:

  STEP 6: Ingest and validate
    - Verify HMAC signature (reject tampered events)
    - Parse GPS coordinates into PostGIS spatial database
    - Publish event to Kafka topic by type (potholes, signs, congestion...)

  STEP 7: Multi-bus confirmation
    - For each incoming detection:
        query spatial index for same location within radius R (default: 10m)
        and time window T (default: 72 hours)
        IF independent detections from >= 2 different buses:
            mark event as VERIFIED
            escalate to dashboard and alert system
        ELSE:
            mark as UNVERIFIED, keep in buffer, await second confirmation
    - Purpose: eliminates false alerts, no single bus detection ever
      reaches official dashboard unconfirmed

  STEP 8: Severity scoring
    - Score each verified event by:
        detection confidence x frequency of reports x road classification
        x estimated vehicle-hours impacted (from traffic flow data)
    - Rank by severity for repair prioritization queue

  STEP 9: Congestion analysis
    - Aggregate vehicle counts per road segment per time window
    - Compute congestion score (low / moderate / high / critical)
    - Flag anomalies (sudden density spike = possible incident)

════════════════════════════════════════
STAGE 3: URBAN INTELLIGENCE OUTPUTS
════════════════════════════════════════

  STEP 10: Dashboard update (real-time)
    - Push verified events to live GIS map (PostGIS + Leaflet/Mapbox)
    - Update heatmaps: pothole density, congestion zones, incident clusters
    - Trigger SMS/email alerts to municipal authority for critical severity events

  STEP 11: Automated repair ticket generation
    - For verified, high-severity road defects:
        auto-generate geo-tagged repair work order
        attach image evidence and detection history
        route to relevant municipal zone authority

  STEP 12: Reporting
    - Daily: zone-wise defect summary, bus coverage map, detection counts
    - Weekly: trend analysis, repair resolution rate, top problem corridors
    - Export formats: PDF report, GeoJSON, CSV for GIS tools

OUTPUT:
  - Live dashboard with real-time city intelligence
  - Verified, geotagged defect and hazard database
  - Automated repair work orders
  - Tamper-proof audit trail per event
  - Cost: ~5 paise per bus per day (edge inference, minimal cloud transfer)