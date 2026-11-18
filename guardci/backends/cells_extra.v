// CLKGATE_X1: integrated clock gater, latch_posedge —
// enable latched while CK low, AND-gated output clock.
module CLKGATE_X1(CK, E, GCK);
  input CK, E;
  output GCK;
  reg en;
  always @(*) if (!CK) en = E;
  assign GCK = CK & en;
endmodule
// sky130_fd_sc_hd__dlclkp_{1,2,4}: integrated clock gater
// (latch_posedge — latch transparent while CLK low, GCLK = CLK & latch).
module sky130_fd_sc_hd__dlclkp_1(CLK, GATE, GCLK);
  input CLK, GATE;
  output GCLK;
  reg en;
  always @(*) if (!CLK) en = GATE;
  assign GCLK = CLK & en;
endmodule
module sky130_fd_sc_hd__dlclkp_2(CLK, GATE, GCLK);
  input CLK, GATE;
  output GCLK;
  reg en;
  always @(*) if (!CLK) en = GATE;
  assign GCLK = CLK & en;
endmodule
module sky130_fd_sc_hd__dlclkp_4(CLK, GATE, GCLK);
  input CLK, GATE;
  output GCLK;
  reg en;
  always @(*) if (!CLK) en = GATE;
  assign GCLK = CLK & en;
endmodule
// sky130_fd_sc_hd__sdlclkp_{1,2,4}: same with scan-control enable —
// latch input is GATE|SCE.
module sky130_fd_sc_hd__sdlclkp_1(CLK, GATE, SCE, GCLK);
  input CLK, GATE, SCE;
  output GCLK;
  reg en;
  always @(*) if (!CLK) en = GATE | SCE;
  assign GCLK = CLK & en;
endmodule
module sky130_fd_sc_hd__sdlclkp_2(CLK, GATE, SCE, GCLK);
  input CLK, GATE, SCE;
  output GCLK;
  reg en;
  always @(*) if (!CLK) en = GATE | SCE;
  assign GCLK = CLK & en;
endmodule
module sky130_fd_sc_hd__sdlclkp_4(CLK, GATE, SCE, GCLK);
  input CLK, GATE, SCE;
  output GCLK;
  reg en;
  always @(*) if (!CLK) en = GATE | SCE;
  assign GCLK = CLK & en;
endmodule
