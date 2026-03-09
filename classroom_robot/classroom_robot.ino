/*
 * Classroom Sound Level Robot (Decibel Version)
 * Using 1088AS LED Matrix Module (MAX7219 driver built in)
 *
 * Shows a face on an 8x8 LED matrix that reacts to noise level:
 *   < 45 dB  -> Big grin face :D   (very quiet)
 *   45-50 dB -> Wink face     ;)   (quiet)
 *   50-55 dB -> Happy face    :)   (quiet talking)
 *   55-60 dB -> Neutral face  :|   (normal talking)
 *   60-65 dB -> Worried face  :(   (getting loud)
 *   65-70 dB -> Shocked face  :O   (loud!)
 *   70-76 dB -> Distressed    D:   (very loud!)
 *   > 76 dB  -> Furious face  D:<  (way too loud!!)
 *
 * Components:
 *   - Arduino Nano
 *   - 1088AS 8x8 LED Matrix Module (with MAX7219)
 *   - 2x 3362 Sound Sensor Modules (the robot's "eyes")
 *   - Touch sensor (3-pin: SIG, VCC, GND)
 *   - Buzzer (2-pin)
 *
 * Wiring:
 *   Matrix:  DIN -> D11, CLK -> D13, CS -> D10, VCC -> 5V, GND -> GND
 *   Mic 1 (left eye):   AO -> A0, VCC -> 5V, GND -> GND
 *   Mic 2 (right eye):  AO -> A1, VCC -> 5V, GND -> GND
 *   Touch sensor:  SIG -> D2, VCC -> 5V, GND -> GND
 *   Buzzer:        + -> D3, - -> GND
 *
 * Library needed: LedControl (install via Library Manager)
 *
 * HOW TO CALIBRATE:
 *   1. Upload this code and open Serial Monitor at 9600 baud
 *   2. Download a free dB meter app on your phone (e.g., "NIOSH SLM")
 *   3. Place your phone next to the robot
 *   4. Compare the dB readings on Serial Monitor to your phone
 *   5. Adjust DB_OFFSET below until the robot's readings roughly
 *      match your phone app
 *   6. Then adjust DB_QUIET, DB_MEDIUM, DB_LOUD to your liking
 */

#include <LedControl.h>
#include <math.h>

// --- Pin Configuration ---
const int DIN_PIN  = 11;   // Matrix Data In
const int CS_PIN   = 10;   // Matrix Chip Select (LOAD)
const int CLK_PIN  = 13;   // Matrix Clock
const int MIC1_PIN  = A0;  // Left eye microphone  (AO pin)
const int MIC2_PIN  = A1;  // Right eye microphone (AO pin)
const int TOUCH_PIN = 2;   // Touch sensor SIG pin
const int BUZZER_PIN = 3;  // Buzzer positive pin

// --- Decibel Thresholds ---
// Typical classroom sound levels for reference:
//   ~40 dB = quiet library / whisper
//   ~55 dB = normal conversation
//   ~70 dB = loud talking / multiple conversations
//   ~85 dB = shouting across the room
const float DB_SILENT    = 45.0;   // Below this = very quiet (big grin)
const float DB_WINK      = 50.0;   // Below this = quiet (wink)
const float DB_QUIET     = 55.0;   // Below this = quiet talking (happy)
const float DB_MEDIUM    = 60.0;   // Below this = normal talking (neutral)
const float DB_LOUD      = 65.0;   // Below this = getting loud (worried)
const float DB_VERY_LOUD = 70.0;   // Below this = loud (shocked)
const float DB_EXTREME   = 76.0;   // Below this = distressed, above = furious

// --- Calibration Offset ---
// This shifts ALL dB readings up or down.
// Increase this number if your readings are too low.
// Decrease this number if your readings are too high.
// Also adjust the 3362 trimpots on your mic modules for best range.
const float DB_OFFSET = 10.0;

// --- Smoothing ---
// Controls how quickly the display reacts (0.0 to 1.0).
// Lower = smoother/slower response (less flickering)
// Higher = faster response (more reactive but may flicker)
const float SMOOTHING = 0.3;

// --- Tickle ---
const int TICKLE_DURATION_MS = 2000;  // How long the tickle lasts

// --- Sampling ---
const int SAMPLE_WINDOW_MS = 50;  // Milliseconds to listen per reading

// --- Internal Constants ---
const float V_REF = 0.005;                // Minimum reference voltage
const float ADC_TO_VOLTS = 5.0 / 1024.0;  // ADC units to volts

// --- Matrix Setup (DIN, CLK, CS, number of devices) ---
LedControl matrix = LedControl(DIN_PIN, CLK_PIN, CS_PIN, 1);

// ============================================================
// Face Patterns (8 rows, each bit = one LED)
//
//   Tip: sketch your own faces on graph paper!
//   Each row is 8 pixels wide. 1 = LED on, 0 = LED off.
// ============================================================

// Big grin face :D  — classroom is very quiet!
const byte FACE_GRIN[8] = {
  0b00000000,
  0b01100110,   //  ##  ##
  0b01100110,   //  ##  ##
  0b00000000,
  0b01111110,   //  ######   <- big open grin
  0b01000010,   //  #    #
  0b01000010,   //  #    #
  0b00111100    //   ####
};

// Wink face ;)  — classroom is quiet
const byte FACE_WINK[8] = {
  0b00000000,
  0b01100110,   //  ##  ##
  0b00100110,   //   #  ##   <- left eye winking
  0b00000000,
  0b00000000,
  0b01000010,   //  #    #
  0b00111100,   //   ####    <- smile
  0b00000000
};

// Happy face :)  — quiet talking
const byte FACE_HAPPY[8] = {
  0b00000000,
  0b01100110,   //  ##  ##
  0b01100110,   //  ##  ##
  0b00000000,
  0b00000000,
  0b01000010,   //  #    #
  0b00111100,   //   ####    <- smile
  0b00000000
};

// Neutral face :|  — getting a little noisy
const byte FACE_NEUTRAL[8] = {
  0b00000000,
  0b01100110,   //  ##  ##
  0b01100110,   //  ##  ##
  0b00000000,
  0b00000000,
  0b00000000,
  0b01111110,   //  ######   <- flat mouth
  0b00000000
};

// Worried face :(  — too loud!
const byte FACE_WORRIED[8] = {
  0b00000000,
  0b01100110,   //  ##  ##
  0b01100110,   //  ##  ##
  0b00000000,
  0b00000000,
  0b00111100,   //   ####    <- frown
  0b01000010,   //  #    #
  0b00000000
};

// Shocked face :O  — way too loud!!
const byte FACE_SHOCKED[8] = {
  0b00000000,
  0b01100110,   //  ##  ##
  0b01100110,   //  ##  ##
  0b00000000,
  0b00011000,   //    ##
  0b00100100,   //   #  #    <- open mouth
  0b00100100,   //   #  #
  0b00011000    //    ##
};

// Distressed face D:  — very loud!
const byte FACE_DISTRESSED[8] = {
  0b00000000,
  0b01100110,   //  ##  ##
  0b01100110,   //  ##  ##
  0b00000000,
  0b01111110,   //  ######   <- flat top of D mouth
  0b01000010,   //  #    #
  0b00100100,   //   #  #
  0b00011000    //    ##     <- curved bottom
};

// Furious face D:<  — way too loud!!
const byte FACE_FURIOUS[8] = {
  0b01000010,   //  #    #   <- angry eyebrows
  0b00100100,   //   #  #
  0b01100110,   //  ##  ##   <- eyes
  0b00000000,
  0b01111110,   //  ######   <- flat top of D mouth
  0b01000010,   //  #    #
  0b00100100,   //   #  #
  0b00011000    //    ##     <- curved bottom
};

// --- State Variables ---
float smoothedDB = 0.0;          // Smoothed decibel reading
int currentLevel = -1;           // Current face being displayed
bool tickled = false;             // Is the robot being tickled?
unsigned long tickleStart = 0;    // When the tickle started

void setup() {
  Serial.begin(9600);

  // Initialize touch sensor and buzzer
  pinMode(TOUCH_PIN, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);

  // Initialize the matrix module
  matrix.shutdown(0, false);    // Wake up the display
  matrix.setIntensity(0, 8);    // Brightness 0 (dim) to 15 (blinding)
  matrix.clearDisplay(0);

  Serial.println(F("=== Classroom Sound Robot (dB Mode) ==="));
  Serial.println(F("Compare these readings to a phone dB meter app."));
  Serial.println(F("Adjust DB_OFFSET in the code to calibrate.\n"));

  // Show the big grin on power-up
  displayFace(FACE_GRIN);
  currentLevel = 0;
}

void loop() {
  // --- Tickle check ---
  if (digitalRead(TOUCH_PIN) == HIGH && !tickled) {
    tickled = true;
    tickleStart = millis();
    displayFace(FACE_GRIN);
    currentLevel = -1;  // Force redraw when tickle ends
    Serial.println(F("\t-> TICKLED! :D hehehe"));
    laughBuzzer();
  }

  // Stay on grin face during tickle duration
  if (tickled) {
    if (millis() - tickleStart < TICKLE_DURATION_MS) {
      delay(100);
      return;  // Skip sound level check while tickled
    }
    tickled = false;  // Tickle is over, resume normal behavior
  }

  // Read peak-to-peak amplitude from both microphone "eyes"
  int raw1 = readPeakToPeak(MIC1_PIN);
  int raw2 = readPeakToPeak(MIC2_PIN);

  // Convert both readings to decibels
  float db1 = peakToDecibels(raw1);
  float db2 = peakToDecibels(raw2);

  // Use the louder of the two microphones
  float dB = max(db1, db2);

  // Apply smoothing to prevent the face from flickering
  // on every small sound change
  smoothedDB = (SMOOTHING * dB) + ((1.0 - SMOOTHING) * smoothedDB);

  // Print to Serial Monitor for calibration
  Serial.print(F("Mic1: "));
  Serial.print(db1, 1);
  Serial.print(F(" dB\tMic2: "));
  Serial.print(db2, 1);
  Serial.print(F(" dB\tSmoothed: "));
  Serial.print(smoothedDB, 1);
  Serial.print(F(" dB"));

  // Decide which face to show based on smoothed dB level
  int newLevel;
  if (smoothedDB < DB_SILENT) {
    newLevel = 0;
    Serial.println(F("\t-> VERY QUIET :D"));
  } else if (smoothedDB < DB_WINK) {
    newLevel = 1;
    Serial.println(F("\t-> QUIET ;)"));
  } else if (smoothedDB < DB_QUIET) {
    newLevel = 2;
    Serial.println(F("\t-> QUIET :)"));
  } else if (smoothedDB < DB_MEDIUM) {
    newLevel = 3;
    Serial.println(F("\t-> MEDIUM :|"));
  } else if (smoothedDB < DB_LOUD) {
    newLevel = 4;
    Serial.println(F("\t-> LOUD :("));
  } else if (smoothedDB < DB_VERY_LOUD) {
    newLevel = 5;
    Serial.println(F("\t-> VERY LOUD :O"));
  } else if (smoothedDB < DB_EXTREME) {
    newLevel = 6;
    Serial.println(F("\t-> VERY LOUD D:"));
  } else {
    newLevel = 7;
    Serial.println(F("\t-> WAY TOO LOUD D:<"));
  }

  // Only update the display when the level changes (avoids flicker)
  if (newLevel != currentLevel) {
    currentLevel = newLevel;
    switch (currentLevel) {
      case 0: displayFace(FACE_GRIN);       break;
      case 1: displayFace(FACE_WINK);       break;
      case 2: displayFace(FACE_HAPPY);      break;
      case 3: displayFace(FACE_NEUTRAL);    break;
      case 4: displayFace(FACE_WORRIED);    break;
      case 5: displayFace(FACE_SHOCKED);    break;
      case 6: displayFace(FACE_DISTRESSED); break;
      case 7: displayFace(FACE_FURIOUS);    break;
    }
  }

  delay(100);  // Short pause between readings
}

/*
 * Reads peak-to-peak sound amplitude from a microphone.
 * Samples the analog pin for SAMPLE_WINDOW_MS milliseconds
 * and returns the difference between the highest and lowest readings.
 */
int readPeakToPeak(int pin) {
  unsigned int signalMax = 0;
  unsigned int signalMin = 1024;
  unsigned long startTime = millis();

  while (millis() - startTime < SAMPLE_WINDOW_MS) {
    unsigned int sample = analogRead(pin);
    if (sample < 1024) {               // Toss out bad readings
      if (sample > signalMax) signalMax = sample;
      if (sample < signalMin) signalMin = sample;
    }
  }

  return signalMax - signalMin;         // Peak-to-peak amplitude
}

/*
 * Converts a peak-to-peak amplitude reading to approximate decibels.
 *
 * The math:
 *   1. Convert ADC value to voltage
 *   2. Calculate dB using: dB = 20 * log10(voltage / reference)
 *   3. Add the calibration offset
 *
 * Note: This gives approximate/relative dB, not lab-grade
 * measurements. That's totally fine for a classroom monitor!
 */
float peakToDecibels(int peakToPeak) {
  // Convert the peak-to-peak ADC value to voltage
  float volts = peakToPeak * ADC_TO_VOLTS;

  // Clamp to minimum so we don't try to take log of zero
  if (volts < V_REF) {
    volts = V_REF;
  }

  // Convert to decibels and add calibration offset
  float dB = 20.0 * log10(volts / V_REF) + DB_OFFSET;

  return dB;
}

/*
 * Plays a "hehehe" laugh on the buzzer using quick
 * alternating high notes. Sounds like a giggle!
 */
void laughBuzzer() {
  // Three quick "he-he-he" bursts
  for (int i = 0; i < 3; i++) {
    tone(BUZZER_PIN, 800, 80);
    delay(100);
    tone(BUZZER_PIN, 1200, 80);
    delay(100);
  }
  // Finish with a higher squeak
  tone(BUZZER_PIN, 1600, 100);
  delay(120);
  noTone(BUZZER_PIN);
}

/*
 * Draws a face pattern on the 8x8 LED matrix.
 * Pass in one of the FACE_ arrays defined above.
 */
void displayFace(const byte face[8]) {
  for (int row = 0; row < 8; row++) {
    matrix.setRow(0, row, face[row]);
  }
}
