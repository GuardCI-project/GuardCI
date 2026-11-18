module gcd (
  input  clk,
  input  reset,
  input  [15:0] a,
  input  [15:0] b,
  output reg [15:0] result
);
  always @(posedge clk) begin
    if (reset) result <= 16'b0;
    else if (a > b) result <= a - b;
    else result <= b - a;
  end
endmodule
