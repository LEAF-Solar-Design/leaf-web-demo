using System;

var start = DateTime.UtcNow;
while (DateTime.UtcNow <= start.AddSeconds(5))
{
}
return new { done = true };
