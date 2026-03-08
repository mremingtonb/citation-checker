/*
 * Classroom Sound Level Robot (Decibel Version)
 *
 * Shows a face on an 8x8 LED matrix that reacts to noise level:
 *   < 50 dB  -> Happy face    :)   (quiet / whisper)
 *   50-65 dB -> Neutral face  :|   (normal talking)
 *   65-80 dB -> Worried face  :(   (loud talking)
 *   > 80 dB  -> Shocked face  :O   (shouting!)
 *
 * Components:
 *   - Arduino Nano
 *   - MAX7219 8x8 LED Matrix
 *   - 2x 3362 Sound Sensor Modules (the robot's "eyes")
 *
 * Wiring:
 *   MAX7219:  DIN -> D11, CLK -> D13, CS -> D10, VCC -> 5V, GND -> GND
 *   Mic 1 (left eye):   AO -> A0, VCC -> 5V, GND -> GND
 *   Mic 2 (right eye):  AO -> A1, VCC -> 5V, GND -> GND
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
const int DIN_PIN  = 11;   // MAX7219 Data In
const int CS_PIN   = 10;   // MAX7219 Chip Select (LOAD)
const int CLK_PIN  = 13;   // MAX7219 Clock
const int MIC1_PIN = A0;   // Left eye microphone  (AO pin)
const int MIC2_PIN = A1;   // Right eye microphone (AO pin)

// --- Decibel Thresholds ---
// Typical classroom sound levels for reference:
//   ~40 dB = quiet library / whisper
//   ~55 dB = normal conversation
//   ~70 dB = loud talking / multiple conversations
//   ~85 dB = shouting across the room
const float DB_QUIET  = 55.0;   // Below this = quiet
const float DB_MEDIUM = 68.0;   // Below this = getting noisy
const float DB_LOUD   = 82.0;   // Below this = loud, above = very loud

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

// --- Sampling ---
const int SAMPLE_WINDOW_MS = 50;  // Milliseconds to listen per reading

// --- Internal Constants ---
const float V_REF = 0.005;              // Minimum reference voltage
const float ADC_TO_VOLTS = 5.0 / 1024.0; // ADC units to volts

// --- Matrix Setup (DIN, CLK, CS, number of devices) ---
LedControl matrix = LedControl(DIN_PIN, CLK_PIN, CS_PIN, 1);

// ============================================================
// Face Patterns (8 rows, each bit = one LED)
//
//   Tip: sketch your own faces on graph paper!
//   Each row is 8 pixels wide. 1 = LED on, 0 = LED off.
// ============================================================

// Happy face :)  — classroom is nice and quiet
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

// --- State Variables ---
float smoothedDB = 0.0;   // Smoothed decibel reading
int currentLevel = -1;    // Current face being displayed

void setup() {
  Serial.begin(9600);

  // Initialize the MAX7219
  matrix.shutdown(0, false);    // Wake up the display
  matrix.setIntensity(0, 8);    // Brightness 0 (dim) to 15 (blinding)
  matrix.clearDisplay(0);

  Serial.println(F("=== Classroom Sound Robot (dB Mode) ==="));
  Serial.println(F("Compare these readings to a phone dB meter app."));
  Serial.println(F("Adjust DB_OFFSET in the code to calibrate.\n"));

  // Show the happy face on power-up
  displayFace(FACE_HAPPY);
  currentLevel = 0;
}

void loop() {
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
  if (smoothedDB < DB_QUIET) {
    newLevel = 0;
    Serial.println(F("\t-> QUIET :)"));
  } else if (smoothedDB < DB_MEDIUM) {
    newLevel = 1;
    Serial.println(F("\t-> MEDIUM :|"));
  } else if (smoothedDB < DB_LOUD) {
    newLevel = 2;
    Serial.println(F("\t-> LOUD :("));
  } else {
    newLevel = 3;
    Serial.println(F("\t-> TOO LOUD :O"));
  }

  // Only update the display when the level changes (avoids flicker)
  if (newLevel != currentLevel) {
    currentLevel = newLevel;
    switch (currentLevel) {
      case 0: displayFace(FACE_HAPPY);   break;
      case 1: displayFace(FACE_NEUTRAL); break;
      case 2: displayFace(FACE_WORRIED); break;
      case 3: displayFace(FACE_SHOCKED); break;
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
 * Draws a face pattern on the 8x8 LED matrix.
 * Pass in one of the FACE_ arrays defined above.
 */
void displayFace(const byte face[8]) {
  for (int row = 0; row < 8; row++) {
    matrix.setRow(0, row, face[row]);
  }
}
