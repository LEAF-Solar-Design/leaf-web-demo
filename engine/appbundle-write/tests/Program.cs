using System;
using System.Text;
using LeafWriteTools;

class Program
{
    static void Main()
    {
        const string valid = "{\"schema\":\"leaf.mutation-plan.v1\",\"added\":[{\"handle\":\"new-1\",\"layer\":\"Test\",\"closed\":true,\"pts\":[[0,0,0],[1,0,0],[0,1,0]]}],\"removed\":[],\"transforms\":[]}";
        var parsed = MutationPlanParser.Parse(Encoding.UTF8.GetBytes(valid));
        if (parsed.Added.Count != 1 || parsed.Sha256.Length != 64) throw new Exception("valid plan failed");
        Console.WriteLine("PASS valid triangle and digest");
        Reject(valid.Replace("\"closed\":true", "\"closed\":false"), "open polyline");
        Reject(valid.Replace("[0,1,0]", "[2,0,0]"), "degenerate geometry");
        Reject(valid.Replace("\"removed\":[]", "\"removed\":[\"new-1\"]"), "invalid DWG handle");
        Reject(valid.Replace("\"schema\":", "\"unknown\":1,\"schema\":"), "unknown field");
        Reject(valid.Replace("\"closed\":true", "\"closed\":true,\"closed\":true"), "duplicate field");
        Reject(valid.Replace("[0,1,0]", "[1e309,1,0]"), "nonfinite coordinate");
        var newline = valid.Replace("new-1", "new-1\\n");
        Reject(newline, "newline in logical handle");
        Reject(valid.Replace("Test", "Test\\n"), "newline in layer");
        Reject(valid.Replace("\"removed\":[]", "\"removed\":[\"AA\\n\"]"), "newline in DWG handle");
    }
    static void Reject(string json, string label)
    {
        try { MutationPlanParser.Parse(Encoding.UTF8.GetBytes(json)); }
        catch (MutationPlanException) { Console.WriteLine("PASS reject " + label); return; }
        throw new Exception("Unexpected acceptance: " + label);
    }
}
